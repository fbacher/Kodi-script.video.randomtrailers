# -*- coding: utf-8 -*-
"""
Created on Feb 12, 2019

@author: Frank Feuerbacher
"""

import sys
import os
import threading
from xml.dom import minidom

import xbmc
import xbmcgui
import xbmcvfs

from common.constants import Constants, Movie
from common.imports import *
from common.exceptions import AbortException
from common.logger import (LazyLogger, Trace)
from common.monitor import Monitor
from common.playlist import Playlist
from common.settings import Settings

from frontend.trailer_dialog import TrailerDialog, DialogState
from frontend.black_background import BlackBackground
from player.player_container import PlayerContainer
from frontend.legal_info import LegalInfo

module_logger = LazyLogger.get_addon_module_logger(file_path=__file__)


'''
    Rough outline:
        Start separate threads to discover basic information about all selected
        video sources:
            1- library
            2- trailer folders
            3- iTunes
            4- TMDB
            5- (future) IMDB
        Each of the above store the discovered info into separate queues.
        The main function here is to discover the identity of all candidates
        for playing so that a balanced mix of trailers is available for playing
        and random selection. It is important to do this quickly. Additional
        information discovery is performed later, in background threads or
        just before playing the video.

        Immediately after starting the discovery threads, the player
        thread is started. The player thread:
            * Loops playing videos until stopped
            * On each iteration it gets movie a to play from
              TrailerManager's ReadyToPlay queue
            * Listens for events:stop & exit, pause, play, queue_movie, showInfo,
              Skip to next trailer, etc.

        TrailerManager holds various queues and lists:
            * Queues for each video source (library, iTunes, etc.) for
                the initial discovery from above
            * Queues for discovering additional information
            * DiscoveredTrailers, a list of all videos after filtering (genre,
                rating, etc). This list grows during initial discovery
            * A small queue (about 5 elements) for each video source so that
                required additional information can be discovered just before
                playing the video. The queues provide enough of a buffer so
                that playing will not be interrupted waiting on discovery
            * The ReadyToPlayQueue which is a small queue containing fully
                discovered trailers and awaiting play. WAs trailers are played
                it is refilled from the small final discovery queues above


'''

logger = LazyLogger.get_addon_module_logger().getChild('frontend.random_trailers_ui')

# noinspection Annotator,PyMethodMayBeStatic,PyRedundantParentheses


def get_title_font():
    # type: () -> str
    """

    :return:
    """
    title_font = 'font13'
    base_size = 20
    multiplier = 1
    skin_dir = xbmcvfs.translatePath("special://skin/")
    list_dir = os.listdir(skin_dir)
    fonts = []
    fontxml_path = ''
    font_xml = ''
    for item in list_dir:
        item = os.path.join(skin_dir, item)
        if os.path.isdir(item):
            font_xml = os.path.join(item, "Font.xml")
        if os.path.exists(font_xml):
            fontxml_path = font_xml
            break
    the_dom = minidom.parse(fontxml_path)
    fontlist = the_dom.getElementsByTagName('font')
    for font in fontlist:
        name = font.getElementsByTagName('name')[0].childNodes[0].nodeValue
        size = font.getElementsByTagName('size')[0].childNodes[0].nodeValue
        fonts.append({'name': name, 'size': float(size)})
    fonts = sorted(fonts, key=lambda k: k['size'])
    for f in fonts:
        if f['name'] == 'font13':
            multiplier = f['size'] / base_size
            break
    for f in fonts:
        if f['size'] >= 38 * multiplier:
            title_font = f['name']
            break
    return title_font


def play_trailers():
    # type: () -> None
    """

    :return:
    """
    my_trailer_dialog = None
    black_background = None
    exiting_playing_movie = False

    # Hack to prevent Kodi from eating first Action (key press)

    kodi_hack()

    try:
        black_background = BlackBackground.get_instance()
        black_background.show()
        license_display_seconds = Settings.get_license_display_seconds()
        if license_display_seconds > 0:
            legal_info = LegalInfo('legal.xml', Constants.ADDON_PATH, 'Default',
                                   license_display_seconds=license_display_seconds)
            legal_info.doModal()
            # Monitor.throw_exception_if_abort_requested(timeout=license_display_seconds)
            legal_info.destroy()
            del legal_info

        my_trailer_dialog = TrailerDialog('script-trailerwindow.xml',
                                          Constants.ADDON_PATH, 'Default')
        exiting_playing_movie = my_trailer_dialog.doModal()
    finally:
        if my_trailer_dialog is not None:
            del my_trailer_dialog
            my_trailer_dialog = None
        if black_background is not None:
            black_background.destroy()
            del black_background
            black_background = None
        if exiting_playing_movie:
            if logger.isEnabledFor(LazyLogger.DEBUG_VERBOSE):
                logger.debug('ReplaceWindow(12005)')
            xbmc.executebuiltin('ReplaceWindow(12005)')


def kodi_hack():
    from player.my_player import MyPlayer
    player = MyPlayer()
    path_to_black_video = Constants.BLACK_VIDEO
    title = 'black'
    listitem = xbmcgui.ListItem(title)
    listitem.setInfo(
        'video', {'title': title, 'genre': 'randomtrailers',
                  'Genre': 'randomtrailers',
                  'trailer': title, 'path': path_to_black_video,
                  'mediatype': 'video', 'tag': 'randomtrailers'})
    listitem.setPath(path_to_black_video)
    player.play(item=path_to_black_video, listitem=listitem)


# noinspection Annotator
class StartUI(threading.Thread):
    """

    """

    def __init__(self, started_as_screesaver):
        # type: (bool) -> None
        """

        :param started_as_screesaver:
        """
        super().__init__(name='startUI')
        self._logger = module_logger.getChild(self.__class__.__name__)

        self._player_container = None
        self._started_as_screensaver = started_as_screesaver

    # Don't start if Kodi is busy playing something

    def run(self):
        # type: () -> None
        """

        :return:
        """
        try:
            self.start_playing_trailers()

        except AbortException:
            return
        except Exception as e:
            self._logger.exception('')

        finally:
            if logger.isEnabledFor(LazyLogger.DEBUG):
                self._logger.debug('Stopping random_trailers player')

            Monitor.abort_requested()

    def start_playing_trailers(self):
        # type: () -> None
        """

        :return:
        """
        # black_background = None
        try:
            if not xbmc.Player().isPlaying() and not self.check_for_xsqueeze():
                if (self._started_as_screensaver and
                        Settings.is_set_fullscreen_when_screensaver()):
                    if not xbmc.getCondVisibility('System.IsFullscreen'):
                        xbmc.executebuiltin('xbmc.Action(togglefullscreen)')

                # TODO: Use settings

                volume_was_adjusted = False
                if Settings.get_adjust_volume():
                    muted = xbmc.getCondVisibility(u"Player.Muted")
                    if not muted and Settings.get_volume() == 0:
                        xbmc.executebuiltin('xbmc.Mute()')
                    else:
                        volume = Settings.get_volume()
                        if volume != 100:
                            volume_was_adjusted = True
                            xbmc.executebuiltin(
                                'XBMC.SetVolume(' + str(volume) + ')')

                self._player_container = PlayerContainer.get_instance()
                play_trailers()

                # TODO: Need to adjust whenever settings changes

                if volume_was_adjusted:
                    muted = xbmc.getCondVisibility('Player.Muted')

                    if muted and Settings.get_volume() == 0:
                        xbmc.executebuiltin('xbmc.Mute()')
                    else:
                        # TODO: Looks fishy, why not set to what it was?

                        current_volume = xbmc.getInfoLabel('Player.Volume')
                        current_volume = int(
                            (float(current_volume.split(' ')[0]) + 60.0) / 60.0 * 100.0)
                        xbmc.executebuiltin(
                            'XBMC.SetVolume(' + str(current_volume) + ')')

                if logger.isEnabledFor(LazyLogger.DEBUG):
                    self._logger.debug('Shutting down')
                Playlist.shutdown()
            else:
                self._logger.info(
                    'Exiting Random Trailers Screen Saver Something is playing!!!!!!')
        except AbortException:
            pass
        except Exception as e:
            self._logger.exception('')

        finally:
            if logger.isEnabledFor(LazyLogger.DEBUG_VERBOSE):
                self._logger.debug_verbose('Stopping xbmc.Player')
            #
            # Player is set to a dummy in the event that it is no longer in
            # Random Trailers control

            if (self._player_container is not None
                    and self._player_container.get_player() is not None):
                self._player_container.get_player().stop()

            black_background = BlackBackground.get_instance()
            if black_background is not None:
                black_background.close()
                black_background.destroy()
                del black_background
            black_background = None

    def check_for_xsqueeze(self):
        # type: () -> bool
        """

        :return:
        """
        self._logger.enter()
        key_map_dest_file = os.path.join(xbmcvfs.translatePath(
            'special://userdata/keymaps'), "xsqueeze.xml")
        if os.path.isfile(key_map_dest_file):
            return True
        else:
            return False
