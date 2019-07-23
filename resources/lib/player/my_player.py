# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import threading
from common.imports import *

from kodi_six import xbmcgui, utils

from player.advanced_player import AdvancedPlayer
from common.logger import (Logger, LazyLogger, Trace)
from common.constants import (Constants, Movie)
from common.disk_utils import DiskUtils
from common.monitor import Monitor
import os

if Constants.INCLUDE_MODULE_PATH_IN_LOGGER:
    module_logger = LazyLogger.get_addon_module_logger().getChild('player.my_player')
else:
    module_logger = LazyLogger.get_addon_module_logger()


# noinspection Annotator
class MyPlayer(AdvancedPlayer):
    """

    """

    def __init__(self):
        # type: () -> None
        """

        """
        super().__init__()
        self._logger = module_logger.getChild(self.__class__.__name__)
        self._expected_title = None
        self._expected_file_path = None
        self._is_url = False
        self._is_activated = True
        self._listener_lock = threading.RLock()
        self._listeners = []

    def play_trailer(self, path, trailer):
        # type: (TextType, MovieType) -> None
        """

        :param path:
        :param trailer:
        :return:
        """
        title = trailer[Movie.TITLE]
        file_path = trailer.get(Movie.NORMALIZED_TRAILER, None)
        if file_path is None:
            file_path = trailer[Movie.TRAILER]
        file_path = utils.py2_decode(file_path)
        file_name = os.path.basename(file_path)
        passed_file_name = utils.py2_decode(os.path.basename(path))
        if file_name != passed_file_name:
            if self._logger.isEnabledFor(Logger.DEBUG):
                self._logger.debug('passed file name:', passed_file_name,
                                    'trailer file_name:', file_name,)

        listitem = xbmcgui.ListItem(title)
        listitem.setInfo(
            'video', {'title': title, 'genre': 'randomtrailers',
                       'Genre': 'randomtrailers',
                       'trailer': passed_file_name, 'path': utils.py2_decode(path),
                       'mediatype': 'video', 'tag': 'randomtrailers'})
        listitem.setPath(file_path)

        self.set_playing_title(title)
        self.set_playing_file_path(file_path)
        if self._logger.isEnabledFor(Logger.DEBUG):
            self._logger.debug('path:', file_name, 'title:', title)

        self.play(item=path, listitem=listitem)

    def play(self, item="", listitem=None, windowed=False, startpos=-1):
        # type: (TextType, xbmcgui.ListItem, bool, int) -> None
        """

        :param item:
        :param listitem:
        :param windowed:
        :param startpos:
        :return:
        """
        super().play(item, listitem, windowed, startpos)

    def set_playing_title(self, title):
        # type: (TextType) ->None
        """

        :param title:
        :return:
        """
        self._expected_title = title

    def set_playing_file_path(self, file_path):
        # type: (TextType) -> None
        """

        :param file_path:
        :return:
        """
        file_path = utils.py2_decode(file_path)
        self._is_url = DiskUtils.is_url(file_path)
        self._expected_file_path = file_path

    def onAVStarted(self):
        # type: () ->None
        """
            Detect when the player is playing something not initiated by this
            script. This can be due to a JSON RPC call or similar.Starting the
            player via keyboard or remote (that does not use JSON RPC)is
            detected by other means (onAction).

            Compare the movie that the player is playing versus what we expect
            it to play. If they don't match, then assume that something else
            launched the movie.

        :return:
        """
        try:
            # All local trailers played by Random Trailers will have a fake genre of
            # 'randomtrailers'. However, if a trailer is from a remote source
            # such that youtube plugin does the actual playing, then the
            # genre will NOT be set to 'randomtrailers'. The use of caching
            # of remote trailers will eliminate this issue.

            genre = utils.py2_decode(self.getVideoInfoTag().getGenre())
            # self._logger.debug('genre:', genre)
            if genre != 'randomtrailers':
                playing_file = super().getPlayingFile()
                playing_file = utils.py2_decode(playing_file)
                if not (self._is_url and DiskUtils.is_url(playing_file)):
                    self._is_activated = False
                    if self._logger.isEnabledFor(Logger.DEBUG):
                        self._logger.debug('Player is playing movie:', playing_file)
                    self.notify_non_random_trailer_video()
        except (Exception) as e:
            pass

    def register_exit_on_movie_playing(self, listener):
        # type: (Callable[[Union[Any, None]], Union[Any, None]]) -> None
        """
            Exit quickly when the player is launched via JSON RPC call, or
            otherwise.
        :param listener:
        :return:
        """
        with self._listener_lock:
            self._listeners.append(listener)

    def notify_non_random_trailer_video(self):
        for listener in self._listeners:
            try:
                listener()
            except (Exception) as e:
                LazyLogger.exception('')

    def dump_data(self, context):
        # type: (TextType) -> None
        """

        :param context:
        :return:
        """
        try:
            if self.isPlayingVideo():
                info_tag_video = self.getVideoInfoTag()
                if self._logger.isEnabledFor(Logger.DEBUG):
                    self._logger.debug('context:', context, 'title:',
                                       info_tag_video.getTitle(),
                                        'genre:', info_tag_video.getGenre(),
                                        'trailer:', info_tag_video.getTrailer())
            else:
                if self._logger.isEnabledFor(Logger.DEBUG):
                    self._logger.debug('Not playing video')
        except (Exception) as e:
            self._logger.exception('')

    def isActivated(self):
        # type: () -> bool
        """

        :return:
        """
        return self._is_activated
