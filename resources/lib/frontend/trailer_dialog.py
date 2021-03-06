# -*- coding: utf-8 -*-

"""
Created on Apr 17, 2019

@author: Frank Feuerbacher
"""

import datetime
import re
import sys
import threading

import xbmc
import xbmcgui
from xbmcgui import (Control, ControlImage, ControlButton, ControlEdit,
                     ControlGroup, ControlLabel, ControlList, ControlTextBox,
                     ControlSpin, ControlSlider, ControlProgress, ControlFadeLabel,
                     ControlRadioButton)

from common.constants import Constants, Movie
from common.debug_utils import Debug
from common.imports import *
from common.playlist import Playlist
from common.exceptions import AbortException
from common.logger import (LazyLogger, Trace, log_entry_exit)
from common.messages import Messages
from common.monitor import Monitor
from common.rating import WorldCertifications
from common.utils import Utils
from action_map import Action
from common.settings import Settings
from player.player_container import PlayerContainer
from frontend.black_background import BlackBackground
from frontend.movie_manager import MovieManager, MovieStatus
from frontend.history_empty import HistoryEmpty

# noinspection PyUnresolvedReferences
from frontend.utils import ReasonEvent
from frontend import text_to_speech

module_logger = LazyLogger.get_addon_module_logger(file_path=__file__)


class DialogState:
    """

    """
    NORMAL = int(0)
    SKIP_PLAYING_TRAILER = int(1)
    GROUP_QUOTA_REACHED = int(2)
    QUOTA_REACHED = int(3)
    NO_TRAILERS_TO_PLAY = int(4)
    USER_REQUESTED_EXIT = int(5)
    START_MOVIE_AND_EXIT = int(6)
    SHUTDOWN_CUSTOM_PLAYER = int(7)
    STARTED_PLAYING_MOVIE = int(8)
    SHUTDOWN = int(9)

    label_map = {NORMAL: 'NORMAL',
                 SKIP_PLAYING_TRAILER: 'SKIP_PLAYING_TRAILER',
                 GROUP_QUOTA_REACHED: 'GROUP_QUOTA_REACHED',
                 QUOTA_REACHED: 'QUOTA_REACHED',
                 NO_TRAILERS_TO_PLAY: 'NO_TRAILERS_TO_PLAY',
                 USER_REQUESTED_EXIT: 'USER_REQUESTED_EXIT',
                 START_MOVIE_AND_EXIT: 'START_MOVIE_AND_EXIT',
                 SHUTDOWN_CUSTOM_PLAYER: 'SHUTDOWN_CUSTOM_PLAYER',
                 STARTED_PLAYING_MOVIE: 'STARTED_PLAYING_MOVIE',
                 SHUTDOWN: 'SHUTDOWN'}

    @staticmethod
    def get_label(dialog_state):
        # type: (DialogState) -> str
        """

        :param dialog_state:
        :return:
        """
        return DialogState.label_map[dialog_state]


# noinspection Annotator,PyMethodMayBeStatic,PyRedundantParentheses
class TrailerDialog(xbmcgui.WindowXMLDialog):
    """
        Note that the underlying 'script-trailer-window.xml' has a "videowindow"
        control. This causes the player to ignore many of the normal keymap actions.
    """
    DETAIL_GROUP_CONTROL = 38001

    DUMMY_TRAILER = {
        Movie.TITLE: '',
        Movie.THUMBNAIL: '',
        Movie.FANART: '',
        Movie.DETAIL_DIRECTORS: '',
        Movie.DETAIL_ACTORS: '',
        Movie.PLOT: '',
        Movie.DETAIL_STUDIOS: ''
    }

    _playlist_map: Dict[int, int] = {xbmcgui.REMOTE_1: 1,
                     xbmcgui.REMOTE_2: 2,
                     xbmcgui.REMOTE_3: 3,
                     xbmcgui.REMOTE_4: 4,
                     xbmcgui.REMOTE_5: 5,
                     xbmcgui.REMOTE_6: 6,
                     xbmcgui.REMOTE_7: 7,
                     xbmcgui.REMOTE_8: 8,
                     xbmcgui.REMOTE_9: 9,
                     xbmcgui.REMOTE_0: 10,

                     xbmcgui.ACTION_JUMP_SMS2: 2,
                     xbmcgui.ACTION_JUMP_SMS3: 3,
                     xbmcgui.ACTION_JUMP_SMS4: 4,
                     xbmcgui.ACTION_JUMP_SMS5: 5,
                     xbmcgui.ACTION_JUMP_SMS6: 6,
                     xbmcgui.ACTION_JUMP_SMS7: 7,
                     xbmcgui.ACTION_JUMP_SMS8: 8,
                     xbmcgui.ACTION_JUMP_SMS9: 9}

    logger: LazyLogger = None

    def __init__(self, *args):
        # type: (*Any) -> None
        """

        :param args:
        """
        super().__init__(*args)
        local_class = TrailerDialog
        if local_class.logger is None:
            local_class.logger = module_logger.getChild(local_class.__name__)
        local_class.logger.enter()
        self._dialog_state = DialogState.NORMAL
        self._player_container = PlayerContainer.get_instance()
        self._player_container.register_exit_on_movie_playing(
            self.exit_screensaver_to_play_movie)

        self.get_player().setCallBacks(on_show_info=self.show_detailed_info)
        self._title_control = None
        self._source = None
        self._movie = None
        self._lock = threading.RLock()
        self._long_trailer_killer = None
        self._viewed_playlist = Playlist.get_playlist(
            Playlist.VIEWED_PLAYLIST_FILE, append=False, rotate=True)
        self._viewed_playlist.add_timestamp()
        self._title_control = None
        self._notification_control = None
        self._notification_timeout = 0.0
        self._notification_killer = None
        self._thread = None
        self._wait_or_interrupt_event = threading.Event()

        # Used mostly as a timer
        self._show_details_event = threading.Event()
        self._wait_event = ReasonEvent()
        Monitor.register_abort_listener(self.on_abort_event)

        self._saved_brief_info_visibility = False
        self._movie_manager = MovieManager()
        self._queued_movie = None
        self._get_next_trailer_start = None
        self.trailers_per_iteration = None
        self.group_trailers = None
        self.total_trailers_to_play = None
        self.delay_between_groups = None
        self.exiting_playing_movie = False

        #
        # Prevent flash of grid
        #
        self.set_visibility(video_window=False, info=False, brief_info=False,
                            notification=False, information=False)

    def onInit(self):
        # type: () -> None
        """

        :return:
        """
        local_class = TrailerDialog
        local_class.logger.enter()

        # Prevent flash of grid
        #
        self.set_visibility(video_window=False, info=False, brief_info=False,
                            notification=False, information=False)
        if self._thread is None:
            # noinspection PyTypeChecker
            self._thread = threading.Thread(
                target=self.play_trailers, name='TrailerDialog')
            self._thread.start()

    def configure_trailer_play_parameters(self):
        # type: () -> None
        """

        :return:
        """
        total_trailers_to_play = Settings.get_number_of_trailers_to_play()

        trailers_per_group = total_trailers_to_play
        group_trailers = Settings.is_group_trailers()

        if group_trailers:
            trailers_per_group = Settings.get_trailers_per_group()

        trailers_per_iteration = total_trailers_to_play
        if trailers_per_group > 0:
            trailers_per_iteration = trailers_per_group
            if total_trailers_to_play > 0:
                trailers_per_iteration = min(
                    trailers_per_iteration, total_trailers_to_play)

        delay_between_groups = Settings.get_group_delay()

        self.trailers_per_iteration = trailers_per_iteration
        self.group_trailers = group_trailers
        self.total_trailers_to_play = total_trailers_to_play
        self.delay_between_groups = delay_between_groups

    def play_trailers(self) -> None:
        """

        :return:
        """
        local_class = TrailerDialog
        self.configure_trailer_play_parameters()
        trailers_played = 0
        try:
            while not self.is_random_trailers_play_state():
                self.play_a_group_of_trailers()

                if self.is_random_trailers_play_state(DialogState.NO_TRAILERS_TO_PLAY):
                    break

                self._player_container.get_player().wait_for_is_not_playing_video()

                # Pre-seed all fields with empty values so that if display of
                # detailed movie information occurs prior to download of external
                # images, etc. This way default values are shown instead of
                # leftovers from previous movie.

                self._movie = TrailerDialog.DUMMY_TRAILER
                self.update_detail_view()  # Does not display

                if self.group_trailers:
                    if self.total_trailers_to_play > 0:
                        trailers_played += self.trailers_per_iteration
                        remaining_to_play = self.total_trailers_to_play - trailers_played
                        if remaining_to_play <= 0:
                            break

                    self._wait_event.wait(self.delay_between_groups)
                    if self.is_random_trailers_play_state(
                            DialogState.USER_REQUESTED_EXIT):
                        break
                    if self.is_random_trailers_play_state(DialogState.NORMAL):
                        # Wake up and resume playing trailers early
                        pass
                    self.set_random_trailers_play_state(DialogState.NORMAL)

                elif self.is_random_trailers_play_state(DialogState.QUOTA_REACHED):
                    break

        except AbortException:
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_VERBOSE):
                local_class.logger.debug_verbose('Received abort')

        except Exception as e:
            local_class.logger.exception('')
        finally:
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose('About to close TrailerDialog')

            self.cancel_long_playing_trailer_killer()
            # local_class.logger.debug('Stopped xbmc.Player')

            self._viewed_playlist.close()
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose('Closed TrailerDialog')
            self.shutdown()
            return  # Exit thread

    def play_a_group_of_trailers(self):
        # type: () -> None
        """

        :return:
        """
        local_class = TrailerDialog
        self.set_visibility(video_window=False, info=False, brief_info=False,
                            notification=False, information=False)
        number_of_trailers_played = 0
        try:
            # Main trailer playing loop

            if Settings.get_show_curtains():
                self._movie_manager.play_curtain_next(
                    MovieManager.OPEN_CURTAIN)

            while not self.is_random_trailers_play_state():
                if self.is_random_trailers_play_state(DialogState.SKIP_PLAYING_TRAILER,
                                                      exact_match=True):
                    self._dialog_state = DialogState.NORMAL

                # Blank the screen

                self.set_visibility(video_window=False, info=False, brief_info=False,
                                    notification=False, information=False)

                # Get the video (or curtain) to display
                try:
                    self._get_next_trailer_start = datetime.datetime.now()
                    status, self._movie = self._movie_manager.get_next_trailer()
                    # if status == MovieStatus.PREVIOUS_MOVIE:
                    #     msg = Messages.get_msg(
                    #         Messages.PLAYING_PREVIOUS_MOVIE)
                    #     msg = msg % self._movie[Movie.TITLE]
                    #     self.notification(msg)
                except (HistoryEmpty):
                    msg = Messages.get_msg(
                        Messages.NO_MORE_MOVIE_HISTORY)
                    self.notification(msg)
                    continue

                # Are there no trailers to play now, and in the future?

                if status == MovieStatus.OK and self._movie is None:
                    self.set_random_trailers_play_state(
                        DialogState.NO_TRAILERS_TO_PLAY)
                    break

                elif status == MovieStatus.IDLE:
                    self.set_random_trailers_play_state(
                        DialogState.NO_TRAILERS_TO_PLAY)
                    local_class.logger.warning('Should not get state IDLE')
                    break

                # TODO: User feedback instead of blank screen?

                if status == MovieStatus.TIMED_OUT:
                    continue

                if status == MovieStatus.BUSY:
                    continue

                if self.is_random_trailers_play_state(DialogState.SKIP_PLAYING_TRAILER,
                                                      exact_match=True):
                    continue

                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_VERBOSE):
                    local_class.logger.debug_verbose('got trailer to play: ' +
                                             self._movie.get(Movie.TRAILER))

                video_is_curtain = (self._movie[Movie.SOURCE] == 'curtain')

                # TODO: fix comment
                # Screen will be black since both
                # TrailerDialog.PLAYER_GROUP_CONTROL and
                # TrailerDialog.DETAIL_GROUP_CONTROL are, by default,
                # not visible in script-trailerwindow.xml

                # Wait until previous video is complete.
                # Our event listeners will stop the player, as appropriate.

                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                    local_class.logger.debug_extra_verbose('wait for not playing 1')
                self.get_player().wait_for_is_not_playing_video()

                # Determine if Movie Information is displayed prior to trailer

                self._source = self._movie.get(Movie.SOURCE)
                show_movie_details = (
                    Settings.get_time_to_display_detail_info() > 0)

                # Determine if Movie Title is to be displayed during play of
                # trailer

                show_movie_title = Settings.get_show_movie_title()

                # Add trailer to "playlist"

                if not video_is_curtain:
                    self._viewed_playlist.record_played_trailer(self._movie)

                # Trailers from a folder are ill-structured and have no
                # identifying information.

                if self._source == Movie.FOLDER_SOURCE:
                    show_movie_details = False

                if video_is_curtain:
                    show_movie_details = False
                    show_movie_title = False

                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                    local_class.logger.debug_extra_verbose('checking play_state 1')
                if self.is_random_trailers_play_state(DialogState.SKIP_PLAYING_TRAILER,
                                                      exact_match=True):
                    continue
                if self.is_random_trailers_play_state():
                    if local_class.logger.isEnabledFor(LazyLogger.DISABLED):
                        local_class.logger.debug_extra_verbose(
                            'breaking due to play_state 1 movie:',
                            self._movie[Movie.TITLE])
                    break

                # This will block if showing Movie Details
                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                    local_class.logger.debug_extra_verbose(
                        'about to show_movie_info movie:',
                        self._movie[Movie.TITLE])
                self.show_movie_info(show_detail_info=show_movie_details,
                                     show_brief_info=show_movie_title)
                if local_class.logger.isEnabledFor(LazyLogger.DISABLED):
                    local_class.logger.debug_extra_verbose(
                        'finished show_movie_info, movie:',
                        self._movie[Movie.TITLE])

                # Play Trailer

                # TODO: change to asynchronous so that it can occur while
                # showing details

                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                    local_class.logger.debug_extra_verbose('checking play_state 2 movie:',
                                             self._movie[Movie.TITLE])
                if self.is_random_trailers_play_state(DialogState.SKIP_PLAYING_TRAILER,
                                                      exact_match=True):
                    continue
                if self.is_random_trailers_play_state(
                        minimum_exit_state=DialogState.USER_REQUESTED_EXIT):
                    if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                        local_class.logger.debug_extra_verbose(
                            'breaking due to play_state 2 movie:',
                            self._movie[Movie.TITLE])
                    break

                if local_class.logger.isEnabledFor(LazyLogger.DISABLED):
                    local_class.logger.debug_extra_verbose('About to play:',
                                             self._movie.get(Movie.TRAILER))

                self.set_visibility(video_window=True, info=False,
                                    brief_info=show_movie_title,
                                    notification=False,
                                    information=show_movie_title)
                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                    local_class.logger.debug_extra_verbose('About to play trailer:',
                                             self._movie[Movie.TITLE])
                normalized = False
                cached = False
                trailer_path = None
                if self._movie.get(Movie.NORMALIZED_TRAILER) is not None:
                    trailer_path = self._movie[Movie.NORMALIZED_TRAILER]
                    self.get_player().play_trailer(trailer_path, self._movie)
                    normalized = True
                elif self._movie.get(Movie.CACHED_TRAILER) is not None:
                    trailer_path = self._movie[Movie.CACHED_TRAILER]
                    self.get_player().play_trailer(trailer_path, self._movie)
                    cached = True
                else:
                    trailer_path = self._movie[Movie.TRAILER]
                    self.get_player().play_trailer(trailer_path, self._movie)

                time_to_play_trailer = (datetime.datetime.now() -
                                        self._get_next_trailer_start)
                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                    local_class.logger.debug_extra_verbose('started play_trailer:',
                                             self._movie[Movie.TITLE],
                                             'elapsed seconds:',
                                             time_to_play_trailer.total_seconds(),
                                             'source:', self._movie[Movie.SOURCE],
                                             'normalized:', normalized,
                                             'cached:', cached,
                                             'path:', trailer_path)

                # Again, we rely on our listeners to interrupt, as
                # appropriate. Trailer/Movie should be about to be played or
                # playing.

                try:
                    if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                        local_class.logger.debug_extra_verbose(
                            'checking play_state 3 movie:',
                            self._movie[Movie.TITLE])
                    if self.is_random_trailers_play_state(
                            DialogState.SKIP_PLAYING_TRAILER,
                            exact_match=True):
                        continue
                    if self.is_random_trailers_play_state(
                            minimum_exit_state=DialogState.USER_REQUESTED_EXIT):
                        if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                            local_class.logger.debug_extra_verbose(
                                'breaking at play_state 3 movie:',
                                self._movie[Movie.TITLE])
                        break

                    if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                        local_class.logger.debug_extra_verbose(
                            'wait_for_is_playing_video 2 movie:',
                            self._movie[Movie.TITLE])
                    if not self.get_player().waitForIsPlayingVideo(timeout=5.0):
                        if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                            local_class.logger.debug_extra_verbose(
                                'Timed out Waiting for Player.')

                    if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                        local_class.logger.debug_extra_verbose(
                            'checking play_state 4 movie:',
                            self._movie[Movie.TITLE])
                    if self.is_random_trailers_play_state(
                            DialogState.SKIP_PLAYING_TRAILER,
                            exact_match=True):
                        continue
                    if self.is_random_trailers_play_state(
                            minimum_exit_state=DialogState.USER_REQUESTED_EXIT):
                        if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                            local_class.logger.debug_extra_verbose(
                                'breaking at play_state 4 movie:',
                                self._movie[Movie.TITLE])
                        break

                    # Now that the trailer has started, see if it will run too long so
                    # that we need to set up to kill it playing.

                    trailer_total_time = self.get_player().getTotalTime()
                    max_play_time = Settings.get_max_trailer_length()
                    if trailer_total_time > max_play_time:
                        if local_class.logger.isEnabledFor(LazyLogger.DEBUG_VERBOSE):
                            local_class.logger.debug_verbose(
                                'Killing long trailer:',
                                self._movie[Movie.TITLE], 'limit:',
                                max_play_time)
                        self.start_long_trailer_killer(max_play_time)
                except AbortException:
                    raise sys.exc_info()
                except Exception as e:
                    local_class.logger.exception('')

                # Again, we rely on our listeners to  stop the player, as
                # appropriate

                self.get_player().wait_for_is_not_playing_video()
                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                    local_class.logger.debug_extra_verbose(
                        'checking play_state 5 movie:',
                        self._movie[Movie.TITLE])
                if self.is_random_trailers_play_state(DialogState.SKIP_PLAYING_TRAILER,
                                                      exact_match=True):
                    continue
                if self.is_random_trailers_play_state(
                        minimum_exit_state=DialogState.USER_REQUESTED_EXIT):
                    if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                        local_class.logger.debug_extra_verbose(
                            'breaking at play_state 5 movie:',
                            self._movie[Movie.TITLE])
                    break

                self.cancel_long_playing_trailer_killer()

                # Again, we rely on our listeners to  stop this display, as
                # appropriate

                self.set_visibility(video_window=False, info=False, brief_info=False,
                                    notification=False, information=False)
                self.configure_trailer_play_parameters()
                if self.trailers_per_iteration != 0 and not video_is_curtain:
                    number_of_trailers_played += 1
                    if number_of_trailers_played > self.trailers_per_iteration:
                        if Settings.is_group_trailers():
                            self.set_random_trailers_play_state(
                                DialogState.GROUP_QUOTA_REACHED)
                        else:
                            self.set_random_trailers_play_state(
                                DialogState.QUOTA_REACHED)

            if self._movie is None:
                local_class.logger.error('There will be no trailers to play')
                self.notification(Messages.get_msg(
                    Messages.NO_TRAILERS_TO_PLAY))
                self.set_random_trailers_play_state(
                    DialogState.NO_TRAILERS_TO_PLAY)
            else:
                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                    local_class.logger.debug_extra_verbose(
                        'out of inner play loop movie:',
                        self._movie[Movie.TITLE])

            if Settings.get_show_curtains():
                self._movie_manager.play_curtain_next(
                    MovieManager.CLOSE_CURTAIN)

                _, curtain = self._movie_manager.get_next_trailer()
                self.set_visibility(video_window=True, info=False, brief_info=False,
                                    notification=False, information=False)
                self.get_player().play_trailer(curtain[Movie.TRAILER],
                                               curtain)
                if not self.get_player().waitForIsPlayingVideo(timeout=5.0):
                    if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                        local_class.logger.debug_extra_verbose(
                            'Timed out Waiting for Player.')
                self.get_player().wait_for_is_not_playing_video()

            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose(
                    'Completed everything except play_movie, if there is one')
        except AbortException:
            reraise(*sys.exc_info())
        except Exception as e:
            local_class.logger.exception('')

        try:
            if self._movie is not None:
                if local_class.logger.isEnabledFor(LazyLogger.DISABLED):
                    local_class.logger.debug_extra_verbose(
                        'Checking to see if there is a movie to play:',
                        self._movie[Movie.TITLE])
            if self.is_random_trailers_play_state(DialogState.START_MOVIE_AND_EXIT,
                                                  exact_match=True):
                if local_class.logger.isEnabledFor(LazyLogger.DISABLED):
                    local_class.logger.debug_extra_verbose(
                        'about to play movie:', self._queued_movie)
                self.set_visibility(video_window=True, info=False, brief_info=False,
                                    notification=False, information=False)
                self.play_movie(self._queued_movie)

        except AbortException:
            local_class.logger.debug('Received shutdown or abort')
        except Exception as e:
            local_class.logger.exception('')

    def get_player(self):
        return self._player_container.get_player()

    def is_random_trailers_play_state(self,
                                      minimum_exit_state: int =
                                      DialogState.GROUP_QUOTA_REACHED,
                                      exact_match: bool = False,
                                      throw_exception_on_abort: bool = True
                                      ) -> bool:
        """
            Checks the current state of random trailers plugin against default
            or passed in values.

            Note that a check for Abort state is performed on each
            call.

            A return value of True indicating whether specified state has been
            reached.

        :param minimum_exit_state: Return True if DialogState is at least this value
        :param exact_match: Only return True if DialogState is exactly this value
        :param throw_exception_on_abort: Throw AbortException
                instead, as appropriate.
        :return:
        """
        local_class = TrailerDialog
        match = False
        if Monitor is None or Monitor.is_abort_requested():
            self._dialog_state = DialogState.SHUTDOWN

        if self._dialog_state == DialogState.SHUTDOWN:
            if throw_exception_on_abort and Monitor is not None:
                Monitor.throw_exception_if_abort_requested()
            else:
                match = True
        elif exact_match:
            match = self._dialog_state == minimum_exit_state
        else:
            match = self._dialog_state >= minimum_exit_state
        return match

    def show_movie_info(self, show_detail_info=False, show_brief_info=False):
        # type: (bool, bool) -> None
        """

        :param show_detail_info:
        :param show_brief_info:
        :return:
        """
        local_class = TrailerDialog
        if show_detail_info:
            self.show_detailed_info()
        else:
            self.hide_detail_info()
        #
        # You can have both showMovieDetails (movie details screen
        # shown prior to playing trailer) as well as the
        # simple ShowTrailerTitle while the trailer is playing.
        #
        if show_brief_info:
            title = self.get_title_string(self._movie)
            self.set_title_control_label(title)
            if not show_detail_info:
                text_to_speech.say_text(title, interrupt=True)

        self.set_visibility(video_window=False, info=show_detail_info,
                            brief_info=show_brief_info,
                            notification=False,
                            information=show_brief_info | show_detail_info)
        pass

    def notification(self, message):
        # type: (str) -> None
        """

        :param message: Message to display
        :return:
        """
        local_class = TrailerDialog
        try:
            #if local_class.logger.isEnabledFor(LazyLogger.DEBUG):
            #    local_class.logger.debug('message:', message)
            self.post_notification(text=message)
            self.set_visibility(notification=True, information=True)
            self.wait_or_interrupt(
                timeout=Constants.MAX_PLAY_TIME_WARNING_TIME)
            self.set_visibility(notification=False)
        except Exception as e:
            local_class.logger.exception('')

        return

    def wait_or_interrupt(self, timeout=0):
        # type: (float) -> None
        """

        :param timeout:
        :return:
        """
        local_class = TrailerDialog

        # During Abort, Monitor deletes itself, so to avoid a silly error in
        # the log, avoid calling it.

        self._wait_or_interrupt_event.clear()
        self._wait_or_interrupt_event.wait(timeout=timeout)
        self._wait_or_interrupt_event.clear()

        return

    def show_detailed_info(self, from_user_request=False):
        # type: (bool) -> None
        """

        :param from_user_request:
        :return:
        """
        local_class = TrailerDialog

        if self._source != Movie.FOLDER_SOURCE:
            if (local_class.logger.isEnabledFor(LazyLogger.DISABLED)
                    and local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE)):
                local_class.logger.debug('about to show_detailed_info')
            display_seconds = Settings.get_time_to_display_detail_info()
            if from_user_request:
                display_seconds = 0
            else:
                if self.get_player() is not None:
                    self.get_player().pausePlay()

            self.update_detail_view()
            self.voice_detail_view()
            self.show_detail_info(self._movie, display_seconds)

    def show_detail_info(self, movie: MovieType, display_seconds: float = 0) -> None:
        """

        :param movie:
        :param display_seconds:
        :return:
        """
        local_class = TrailerDialog
        self.set_visibility(video_window=False, info=True, brief_info=False,
                            notification=False, information=True)

        if display_seconds == 0:
            # One year
            display_seconds = 365 * 24 * 60 * 60
        Monitor.throw_exception_if_abort_requested()
        self._show_details_event.clear()  # In case it was set
        #
        # A Monitor abort event should set (and unblock) the wait.
        # Also, hide detail view will unblock.
        #
        self._show_details_event.wait(float(display_seconds))
        Monitor.throw_exception_if_abort_requested()
        self._show_details_event.clear()  # In case it was set
        # self.hide_detail_info()
        self._show_details_event.set()  # Force show_detail_info to unblock
        if movie[Movie.SOURCE] == Movie.TFH_SOURCE:
            scroll_plot = False
        else:
            scroll_plot = True
        self.set_visibility(video_window=False, info=False, brief_info=False,
                            notification=False, information=False,
                            scroll_plot=scroll_plot)
        text_to_speech.say_text('.', interrupt=True)

    def hide_detail_info(self, reason=''):
        # type: (str) -> None
        """

        :param reason:
        :return:
        """
        local_class = TrailerDialog
        local_class.logger.enter()
        self._show_details_event.set()  # Force show_detail_info to unblock
        self.set_visibility(info=False, information=False)

    def set_visibility(self,
                       video_window: Union[bool, None] = None,
                       info: Union[bool, None] = None,
                       brief_info: Union[bool, None] = None,
                       notification: Union[bool, None] = None,
                       information: Union[bool, None] = None,
                       scroll_plot: Union[bool, None] = None
                       ) -> None:
        """
            Controls the visible elements of TrailerDialog

        :param video_window:
        :param info:
        :param brief_info:
        :param notification:
        :param information:
        :param scroll_plot: If True, then scroll plot text
        :return:
        """
        local_class = TrailerDialog
        # self.wait_or_interrupt(timeout=0)
        shutdown = False
        try:
            if self.is_random_trailers_play_state(
                    minimum_exit_state=DialogState.SHUTDOWN_CUSTOM_PLAYER):
                shutdown = True
        except AbortException:
            shutdown = True

        if shutdown:
            video_window = True
            info = False
            brief_info = False
            notification = False
            information = False

        commands = []

        nested_controls = [info, brief_info, notification]
        if True in nested_controls:
            # If any of the textual information controls change visibility, then
            # black out before
            # changes within the control are changed and then make visible
            # again at the end, if requested.

            info_command = "Skin.Reset(NonVideo)"
            commands.append(info_command)

        if video_window is not None:
            if video_window:
                video_command = "Skin.SetBool(Video)"
            else:
                video_command = "Skin.Reset(Video)"
            commands.append(video_command)
        if info is not None:
            title = None
            if info:
                title = self.getControl(38003)
                info_command = "Skin.SetBool(Info)"
            else:
                info_command = "Skin.Reset(Info)"
            commands.append(info_command)

        if brief_info is not None:
            title = None
            if brief_info:
                title = self.getControl(38021)
                info_command = "Skin.SetBool(SummaryLabel)"
            else:
                info_command = "Skin.Reset(SummaryLabel)"
            commands.append(info_command)

        if notification is not None:
            if notification:
                info_command = "Skin.SetBool(Notification)"
            else:
                info_command = "Skin.Reset(Notification)"
            commands.append(info_command)

        # The disable case was taken care of above. Handle enable here after
        # everything contained within the group is set

        if information is not None:
            if information:
                info_command = "Skin.SetBool(NonVideo)"
            else:
                info_command = "Skin.Reset(NonVideo)"
            commands.append(info_command)

        if scroll_plot is not None:
            if scroll_plot:
                scroll_plot_command = "Skin.SetBool(ScrollPlot)"
            else:
                scroll_plot_command = "Skin.Reset(ScrollPlot)"
            commands.append(scroll_plot_command)

        for command in commands:
            if local_class.logger.isEnabledFor(LazyLogger.DISABLED):
                local_class.logger.debug_extra_verbose(command)
            # noinspection PyTypeChecker
            xbmc.executebuiltin(command)

    def update_detail_view(self):
        # type: () -> None
        """

        :return:
        """
        local_class = TrailerDialog
        try:
            Monitor.throw_exception_if_abort_requested()
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.enter()

            control: Union[ControlImage, Control] = self.getControl(38002)
            thumbnail = self._movie.get(Movie.THUMBNAIL)
            if thumbnail is None:
                control.setVisible(False)
            else:
                control.setImage(thumbnail)
                control.setVisible(True)

            control: Union[ControlImage, Control] = self.getControl(38004)
            image = self._movie.get(Movie.FANART)
            if image is None:
                control.setVisible(False)
            else:
                control.setVisible(True)
                control.setImage(self._movie[Movie.FANART])

            verbose = False
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                verbose = True
            title_string = self.get_title_string(self._movie, verbose)

            title_control: Union[ControlLabel, Control] = self.getControl(38003)
            title_control.setLabel(title_string)

            # title.setAnimations(
            #    [('Hidden', 'effect=fade end=0 time=1000')])

            control: Union[ControlLabel, Control] = self.getControl(38005)
            movie_directors = self._movie.get(Movie.DETAIL_DIRECTORS)
            if movie_directors is None:
                control.setVisible(False)
            else:
                control.setLabel(movie_directors)
                control.setVisible(True)

            movie_actors = self._movie.get(Movie.DETAIL_ACTORS)
            control: Union[ControlLabel, Control] = self.getControl(38006)
            if movie_actors is None:
                control.setVisible(False)
            else:
                control.setLabel(movie_actors)
                control.setVisible(True)

            control: Union[ControlLabel, Control] = self.getControl(38007)
            movie_writers = self._movie.get(Movie.DETAIL_WRITERS)
            if movie_writers is None:
                control.setVisible(False)
            else:
                control.setLabel(movie_writers)
                control.setVisible(True)

            control: Union[ControlTextBox, Control] = self.getControl(38009)
            plot: str = self._movie[Movie.PLOT]
            if plot is None:
                plot = ''

            cleaned_plot = plot
            if self._movie[Movie.SOURCE] == Movie.TFH_SOURCE:
                patterns = [
                    r'(\n ?\n.*)',
                    # r'\nA(nd, a)?s always, find more great cinematic classics at
                    # http://www.trailersfromhell.com',
                    # r'\n \nAnd, as always, find more cinematic greatness at
                    # http://www.trailersfromhell.com',
                    # r'\n \nAs always, you can find more commentary, more reviews,
                    # more podcasts, and more deep-dives into the films you don\'t know
                    # you love yet over on the Trailers From Hell mothership:',
                    r'(http://www.trailersfromhell.com.*)',
                    r'(ABOUT TRAILERS FROM HELL:.*)',
                    r'(As always, you can find more commentary.*)',
                    r'(But wait! There\'s more! TFH.*)',
                    r'(Want more TFH.*)',
                    r'(Want to know more? The TFH.*)',
                    r'(DID YOU KNOW: we have a podcast.*)'
                ]
                for pattern in patterns:
                    cleaned_plot = re.sub(pattern, r' || \g<1>', plot)
                    if len(cleaned_plot) != len(plot):
                        break

            if cleaned_plot is None:
                control.setVisible(False)
            else:
                control.setText(cleaned_plot)
                control.setVisible(True)

            control: Union[ControlLabel, Control] = self.getControl(38010)
            movie_studios = self._movie.get(Movie.DETAIL_STUDIOS)
            if movie_studios is None:
                control.setVisible(False)
            else:
                control.setLabel(movie_studios)
                control.setVisible(True)

            label = Messages.get_formatted_msg(Messages.RUNTIME_GENRE,
                                               self._movie.get(Movie.DETAIL_RUNTIME, ''),
                                               self._movie.get(Movie.DETAIL_GENRES, ''))
            control: Union[ControlLabel, Control] = self.getControl(38011)
            control.setLabel(label)

            image = 'stars/{:.1f}.png'.format(self._movie.get(Movie.RATING, 0.0))
            rating_control: Union[ControlImage, Control] = self.getControl(38012)
            rating_control.setImage(image)
            rating_control.setColorDiffuse('0xC0FFD700')

            control: Union[ControlImage, Control] = self.getControl(38013)
            image_rating = self._movie.get(Movie.DETAIL_CERTIFICATION_IMAGE)
            if image_rating is None:
                control.setVisible(False)
            else:
                control.setImage(image_rating)
                control.setVisible(True)

            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.exit()

        except AbortException:
            raise sys.exc_info()
        except Exception as e:
            local_class.logger.exception('')
        finally:
            pass

    def voice_detail_view(self):
        # type: () -> None
        """

        :return:
        """
        local_class = TrailerDialog
        try:
            Monitor.throw_exception_if_abort_requested()
            if self.logger.isEnabledFor(LazyLogger.DISABLED):
                local_class.logger.enter()

            title_label = Messages.get_formatted_msg(Messages.TITLE_LABEL)
            text_to_speech.say_text(title_label, interrupt=True)

            title_string = self.get_title_string(self._movie)
            text_to_speech.say_text(title_string, interrupt=False)

            rating = self._movie.get(Movie.RATING, None)
            if rating is None:
                rating = 0.0

            # convert to scale of 5 instead of 10, Round to nearest 0.5

            rating = int(rating * 10) / 20

            # "Rated 4.5 out of 5 stars"
            text_to_speech.say_text(
                Messages.get_formatted_msg(Messages.VOICED_STARS, str(rating)))

            # MPAA rating
            certification = self._movie[Movie.DETAIL_CERTIFICATION]
            text_to_speech.say_text(
                Messages.get_formatted_msg(
                    Messages.VOICED_CERTIFICATION, certification))

            runtime_genres = Messages.get_formatted_msg(
                Messages.RUNTIME_GENRE,
                self._movie[Movie.DETAIL_RUNTIME],
                self._movie[Movie.DETAIL_GENRES])
            text_to_speech.say_text(runtime_genres, interrupt=False)

            director_label = \
                Messages.get_formatted_msg(Messages.DIRECTOR_LABEL)
            text_to_speech.say_text(director_label, interrupt=False)

            movie_directors = self._movie[Movie.DETAIL_DIRECTORS]
            text_to_speech.say_text(movie_directors, interrupt=False)

            writer_label = \
                Messages.get_formatted_msg(Messages.WRITER_LABEL)
            text_to_speech.say_text(writer_label, interrupt=False)

            movie_writers = self._movie[Movie.DETAIL_WRITERS]
            text_to_speech.say_text(movie_writers, interrupt=False)

            stars_label = \
                Messages.get_formatted_msg(Messages.STARS_LABEL)
            text_to_speech.say_text(stars_label, interrupt=False)

            movie_actors = self._movie.get(Movie.VOICED_DETAIL_ACTORS, "")
            text_to_speech.say_text(movie_actors, interrupt=False)

            plot_label = Messages.get_formatted_msg(Messages.PLOT_LABEL)
            text_to_speech.say_text(plot_label, interrupt=False)

            plot: str = self._movie.get(Movie.PLOT)
            if plot is None:
                plot = ''

            cleaned_plot = plot
            if self._movie[Movie.SOURCE] == Movie.TFH_SOURCE:
                patterns = [
                    r'\n ?\n.*',
                    # r'\nA(nd, a)?s always, find more great cinematic classics at
                    # http://www.trailersfromhell.com',
                    # r'\n \nAnd, as always, find more cinematic greatness at
                    # http://www.trailersfromhell.com',
                    # r'\n \nAs always, you can find more commentary, more reviews,
                    # more podcasts, and more deep-dives into the films you don\'t know
                    # you love yet over on the Trailers From Hell mothership:',
                    r'http://www.trailersfromhell.com.*',
                    r'ABOUT TRAILERS FROM HELL:.*',
                    r'As always, you can find more commentary.*',
                    r'But wait! There\'s more! TFH.*',
                    r'Want more TFH.*',
                    r'Want to know more? The TFH.*',
                    r'DID YOU KNOW: we have a podcast.*'
                ]
                for pattern in patterns:
                    cleaned_plot = re.sub(pattern, r'', plot)
                    if len(cleaned_plot) != len(plot):
                        break

            self.logger.debug('Plot original text:', plot)
            self.logger.debug('Plot text:', cleaned_plot)
            text_to_speech.say_text(cleaned_plot, interrupt=False)

            movie_studios = self._movie[Movie.DETAIL_STUDIOS]
            text_to_speech.say_text(movie_studios, interrupt=False)

        except AbortException:
            raise sys.exc_info()
        except Exception as e:
            local_class.logger.exception('')
        finally:
            pass

    def doModal(self) -> bool:
        """

        :return:
        """
        local_class = TrailerDialog

        super().doModal()
        return self.exiting_playing_movie

    def show(self) -> None:
        """

        :return:
        """
        super().show()

    def close(self) -> None:
        """

        :return:
        """
        super().close()

    def set_random_trailers_play_state(self, dialog_state: int) -> None:
        # TODO: Change to use named int type
        """

        :param dialog_state:
        :return:
        """
        local_class = TrailerDialog

        if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
            local_class.logger.debug_extra_verbose('state:',
                                                   DialogState.get_label(dialog_state),
                                                   trace=Trace.TRACE_SCREENSAVER)

        if dialog_state > self._dialog_state:
            self._dialog_state = dialog_state

        if dialog_state >= DialogState.SHUTDOWN_CUSTOM_PLAYER:
            self.get_player().setCallBacks(on_show_info=None)
            self.get_player().disableAdvancedMonitoring()
            self._player_container.use_dummy_player()
            self.set_visibility(video_window=False, info=False,
                                brief_info=False, notification=False)

        if dialog_state >= DialogState.USER_REQUESTED_EXIT:
            # Stop playing trailer.

            # Just in case we are paused
            self.get_player().resumePlay()
            self.kill_long_playing_trailer(inform_user=False)

        # Multiple groups can be played before exiting. Allow
        # them to be reset back to normal.

        if self._dialog_state == DialogState.GROUP_QUOTA_REACHED:
            self._dialog_state = dialog_state

        # if dialog_state > self._dialog_state:
        #     self._dialog_state = dialog_state
        self._wait_event.set(ReasonEvent.RUN_STATE_CHANGE)

    def exit_screensaver_to_play_movie(self):
        # type: () -> None
        """

        :return:
        """
        local_class = TrailerDialog

        self.set_random_trailers_play_state(DialogState.SHUTDOWN_CUSTOM_PLAYER)

        black_background = BlackBackground.get_instance()
        if black_background is not None:
            black_background.set_visibility(opaque=True)
            black_background.close()
            del black_background
            black_background = None

        self.exiting_playing_movie = True
        self.close()
        # noinspection PyTypeChecker
        xbmc.executebuiltin('Action(FullScreen,12005)')

    def on_abort_event(self) -> None:
        """

        :return:
        """
        local_class = TrailerDialog

        local_class.logger.enter()
        self._show_details_event.set()  # Unblock waits
        self.set_random_trailers_play_state(DialogState.SHUTDOWN)
        self._wait_event.set(ReasonEvent.SHUTDOWN)

       # TODO: put this in own class

    def start_long_trailer_killer(self, max_play_time):
        # type: (Union[int, float]) -> None
        """

        :param max_play_time:
        :return:
        """
        local_class = TrailerDialog

        if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
            local_class.logger.debug_extra_verbose('waiting on lock',
                                     trace=Trace.TRACE_UI_CONTROLLER)
        with self._lock:
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose('got lock, max_play_time:',
                                         max_play_time,
                                         trace=Trace.TRACE_UI_CONTROLLER)
            self._long_trailer_killer = None
            if not self.is_random_trailers_play_state(DialogState.USER_REQUESTED_EXIT):
                if max_play_time > Constants.MAX_PLAY_TIME_WARNING_TIME + 2:
                    max_play_time -= Constants.MAX_PLAY_TIME_WARNING_TIME
                    local_class.logger.debug('adjusted max_play_time:', max_play_time,
                                             trace=Trace.TRACE_UI_CONTROLLER)
                self._long_trailer_killer = threading.Timer(max_play_time,
                                                            self.kill_long_playing_trailer)
                # noinspection PyTypeChecker
                self._long_trailer_killer.setName('TrailerKiller')
                self._long_trailer_killer.start()

    def kill_long_playing_trailer(self, inform_user=True):
        # type: (bool) -> None
        """

        :param inform_user:
        :return:
        """
        local_class = TrailerDialog

        try:
            local_class.logger.enter()
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose('Now Killing',
                                         trace=Trace.TRACE_UI_CONTROLLER)

            if inform_user:
                self.notification(Messages.get_msg(
                    Messages.TRAILER_EXCEEDS_MAX_PLAY_TIME))

            self.get_player().stop()

            with self._lock:
                self._long_trailer_killer = None
        except AbortException:
            raise sys.exc_info()
        except Exception as e:
            local_class.logger.exception(msg='')

        if local_class.logger.isEnabledFor(LazyLogger.DISABLED):
            local_class.logger.debug('exit', trace=Trace.TRACE_SCREENSAVER)

    def cancel_long_playing_trailer_killer(self):
        # type: () -> None
        """

        :return:
        """
        local_class = TrailerDialog
        if local_class.logger.isEnabledFor(LazyLogger.DISABLED):
            local_class.logger.debug('enter, waiting on lock',
                                     trace=[Trace.TRACE_SCREENSAVER,
                                            Trace.TRACE_UI_CONTROLLER])
        with self._lock:
            if self._long_trailer_killer is not None:
                self._long_trailer_killer.cancel()

    def play_next_trailer(self):
        # type: () -> None
        """

        :return:
        """
        local_class = TrailerDialog
        local_class.logger.enter()

        # If idle due to wait between trailer groups, then interrupt
        # and play next trailer.

        if self.is_random_trailers_play_state(DialogState.GROUP_QUOTA_REACHED,
                                              exact_match=True):
            # Wake up wait in between groups
            self.set_random_trailers_play_state(DialogState.NORMAL)

        self.cancel_long_playing_trailer_killer()
        self.hide_detail_info()
        if self.get_player() is not None:
            self.get_player().stop()
        if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
            local_class.logger.debug_extra_verbose('Finished playing trailer',
                                     trace=Trace.TRACE_SCREENSAVER)

    def getFocus(self):
        # type: () -> None
        """

        :return:
        """
        local_class = TrailerDialog
        if local_class.logger.isEnabledFor(LazyLogger.DEBUG):
            local_class.logger.debug('Do not use.')
        return

    def onAction(self, action: xbmcgui.Action) -> None:
        """

        :param action:
        :return:

            SHOW_INFO -> Toggle Display custom InfoDialog

            STOP -> Skip to next trailer
            ACTION_MOVE_RIGHT -> Skip to next trailer

            ACTION_MOVE_LEFT -> Play previous trailer

            PREVIOUS_MENU | NAV_BACK | ACTION_BUILT_IN_FUNCTION ->
                                                 Exit Random Trailer script
                or stop Screensaver

            PAUSE -> Toggle Play/Pause playing trailer
            PLAY -> Toggle Play/Pause playing trailer

            ENTER -> Play movie for current trailer (if available)

            REMOTE_0 .. REMOTE_9 -> Record playing movie info to
                        userdata/addon_data/script.video.randomtrailers/<playlist<n>

            ACTION_QUEUE_ITEM -> Add movie to Couch Potato
        """
        local_class = TrailerDialog

        # Grab handle to movie, it might go away.

        movie = self._movie
        if action.getId() != 107:  # Mouse Move
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose('Action.id:', action.getId(),
                                         hex(action.getId()),
                                         'Action.button_code:',
                                         action.getButtonCode(),
                                         hex(action.getButtonCode()), trace=Trace.TRACE)

        action_mapper = Action.get_instance()
        matches = action_mapper.getKeyIDInfo(action)

        # Mouse Move
        if action.getId() != 107 and local_class.logger.isEnabledFor(
                LazyLogger.DEBUG_EXTRA_VERBOSE):
            for line in matches:
                local_class.logger.debug_extra_verbose(line)

        action_id = action.getId()
        button_code = action.getButtonCode()

        # These return empty string if not found
        action_key = action_mapper.getActionIDInfo(action)
        remote_button = action_mapper.getRemoteKeyButtonInfo(action)
        remote_key_id = action_mapper.getRemoteKeyIDInfo(action)

        # Returns found button_code, or 'key_' +  action_button
        action_button = action_mapper.getButtonCodeId(action)

        separator = ''
        key = ''
        if action_key != '':
            key = action_key
            separator = ', '
        if remote_button != '':
            key = key + separator + remote_button
            separator = ', '
        if remote_key_id != '':
            key = key + separator + remote_key_id
        if key == '':
            key = action_button
        # Mouse Move
        if action.getId() != 107 and local_class.logger.isEnabledFor(
                LazyLogger.DEBUG_EXTRA_VERBOSE):
            local_class.logger.debug_extra_verbose('Key found:', key)

        #################################################################
        #   ACTIONS
        ##################################################################
        #    DEBUG thread dump
        #################################################################

        if (local_class.logger.isEnabledFor(LazyLogger.DEBUG_VERBOSE)
                and (action_id == xbmcgui.ACTION_PAGE_UP
                    or action_id == xbmcgui.ACTION_MOVE_UP)):

            from common.debug_utils import Debug
            Debug.dump_all_threads()

        if action_id == xbmcgui.ACTION_SHOW_INFO:
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose(key, 'Toggle Show_Info')

            if not self.is_random_trailers_play_state(DialogState.NORMAL):
                heading = Messages.get_msg(Messages.HEADER_IDLE)
                message = Messages.get_msg(Messages.PLAYER_IDLE)
                self.notification(message)
            elif self.getControl(TrailerDialog.DETAIL_GROUP_CONTROL).isVisible():
                self.hide_detail_info()
                self.get_player().resumePlay()
            else:
                self.show_detailed_info(from_user_request=True)

        ##################################################################
        elif (action_id == xbmcgui.ACTION_STOP or action_id == xbmcgui.ACTION_MOVE_RIGHT):
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose(
                    key, 'Play next trailer at user\'s request')
            self._wait_or_interrupt_event.set()
            self.set_random_trailers_play_state(
                DialogState.SKIP_PLAYING_TRAILER)
            self.play_next_trailer()

        ##################################################################

        elif action_id == xbmcgui.ACTION_MOVE_LEFT:
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose(key,
                                         'Play previous trailer at user\'s request')
            self._wait_or_interrupt_event.set()
            self._movie_manager.play_previous_trailer()
            self.set_random_trailers_play_state(
                DialogState.SKIP_PLAYING_TRAILER)
            self.play_next_trailer()

        ##################################################################
        #
        # PAUSE/PLAY is handled by native player
        #
        elif action_id == xbmcgui.ACTION_QUEUE_ITEM:
            if Utils.is_couch_potato_installed() and movie is not None:
                if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                    local_class.logger.debug_extra_verbose(key, 'Queue to couch potato')
                str_couch_potato = \
                    'plugin://plugin.video.couchpotato_manager/movies/add?title=' + \
                    movie[Movie.TITLE]
                xbmc.executebuiltin('XBMC.RunPlugin(' + str_couch_potato + ')')

        ##################################################################
        elif (action_id == xbmcgui.ACTION_PREVIOUS_MENU
              or action_id == xbmcgui.ACTION_NAV_BACK):
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose('Exit application',
                                         trace=Trace.TRACE_SCREENSAVER)
                local_class.logger.debug_extra_verbose(
                    key, 'Exiting RandomTrailers at user request')

            # Ensure we are not blocked

            self.hide_detail_info()
            self.set_random_trailers_play_state(
                DialogState.USER_REQUESTED_EXIT)
        ##################################################################

        # TODO: Need proper handling of this (and other inputs that we don't
        # handle. Sigh

        elif action_id == xbmcgui.ACTION_BUILT_IN_FUNCTION:
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_VERBOSE):
                local_class.logger.debug_verbose(key, 'Exiting RandomTrailers due to',
                                                 'ACTION_BUILT_IN_FUNCTION',
                                                 trace=Trace.TRACE_SCREENSAVER)

            # Ensure we are not blocked

            self.hide_detail_info()
            self.set_random_trailers_play_state(
                DialogState.USER_REQUESTED_EXIT)

        ##################################################################
        elif (action_id == xbmcgui.ACTION_ENTER
              or action_id == xbmcgui.ACTION_SELECT_ITEM
              or action_id == xbmcgui.ACTION_SHOW_GUI) and movie is not None:
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose(key, 'Play Movie')
            movie_file = movie[Movie.FILE]
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose(
                    'Playing movie for currently playing trailer.',
                    'movie_file:', movie_file, 'source:',
                    self._movie[Movie.SOURCE])
            if movie_file is None or movie_file == '':
                heading = Messages.get_msg(Messages.HEADING_INFO)
                message = Messages.get_msg(Messages.NO_MOVIE_TO_PLAY)
                self.notification(message)
            elif not self.is_random_trailers_play_state(DialogState.NORMAL):
                heading = Messages.get_msg(Messages.HEADER_IDLE)
                message = Messages.get_msg(Messages.PLAYER_IDLE)
                self.notification(message)
            else:
                self.queue_movie(movie)

        ##################################################################
        # From IR remote as well as keyboard
        # Close InfoDialog and resume playing trailer

        elif action_id in TrailerDialog._playlist_map and movie is not None:
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose(key)
            self.add_to_playlist(action_id, movie)

    def set_title_control_label(self, text=''):
        # type: (str) -> None
        """

        :param text:
        :return:
        """
        local_class = TrailerDialog

        title_control = self.getControl(38021)
        if text != '':
            title_control.setLabel(text)
        return

    def post_notification(self, text=None):
        # type: (str) -> None
        """

        :param text:
        :return:
        """
        local_class = TrailerDialog

        notification_control: Union[Control, ControlLabel] = self.getControl(38023)
        notification_control_2: Union[Control, ControlLabel] = self.getControl(38024)

        if text != '':
            bold_text = '[B]' + text + '[/B]'
            notification_control.setLabel(bold_text)
            notification_control_2.setLabel(bold_text)
            text_to_speech.say_text(text, interrupt=True)
        return

    def add_to_playlist(self, action_id: int, movie: MovieType) -> None:
        """

        :param action_id:
        :param movie:
        :return:
        """
        local_class = TrailerDialog
        playlist_number = TrailerDialog._playlist_map[action_id]
        playlist_name = Settings.get_playlist_name(playlist_number)
        if playlist_name is None or playlist_name == '':
            local_class.logger.error(
                'Invalid playlistId, ignoring request to write to playlist.')
        else:
            added = Playlist.get_playlist(playlist_name, playlist_format=True).\
                add_to_smart_playlist(movie)
            if added:
                self.notification(Messages.get_formatted_msg(
                    Messages.MOVIE_ADDED_TO_PLAYLIST, playlist_name))
            else:
                self.notification(Messages.get_formatted_msg(
                    Messages.MOVIE_ALREADY_ON_PLAYLIST, playlist_name))

    def queue_movie(self, movie):
        # type: (Dict[str, str]) -> None
        """
            At user request, queue movie to be played after canceling play
            of current movie, closing curtain and closing customer Player.

        :param movie:
        :return:
        """
        local_class = TrailerDialog

        if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
            local_class.logger.debug_extra_verbose('Queuing movie at user request:',
                                                   movie[Movie.TITLE])
        self._queued_movie = movie
        self.set_random_trailers_play_state(DialogState.START_MOVIE_AND_EXIT)

        # Unblock Detail Info display

        self.hide_detail_info()

    def play_movie(self, movie, already_playing=False):
        # type: (Dict[str, str], bool) -> None
        """
            At user request, start playing movie on normal xbmc.player, after
            disabling the custom player that we use here.

            When already-playing is True, then the user has externally (JSON-RPC)
            started a movie and we just need to get out of the way.

        :param movie:
        :param already_playing: True when movie externally started and we need
                                to get the heck out of the way
        :return:
        """
        local_class = TrailerDialog

        black_background = BlackBackground.get_instance()
        black_background.set_visibility(opaque=False)
        black_background.close()
        black_background.destroy()

        if not already_playing:
            movie_file = movie[Movie.FILE]
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose('Playing movie at user request:',
                                                       movie[Movie.TITLE],
                                                       'path:', movie_file)

            self.set_random_trailers_play_state(
                DialogState.SHUTDOWN_CUSTOM_PLAYER)
            xbmc.Player().play(movie_file)

        if Monitor.is_abort_requested():
            if local_class.logger.isEnabledFor(LazyLogger.DEBUG_EXTRA_VERBOSE):
                local_class.logger.debug_extra_verbose(
                    'ABORT requested before playing movie!')
        while not Monitor.wait_for_abort(timeout=0.10):
            # Call xbmc.Player directly to avoid using DummyPlayer
            if xbmc.Player().isPlayingVideo():
                break

        self.set_random_trailers_play_state(DialogState.STARTED_PLAYING_MOVIE)

        # Time to exit plugin
        Monitor.abort_requested()
        local_class.logger.exit('Just started player')

    def get_title_string(self, movie: MovieType, verbose: bool = False) -> str:
        """

        :param movie:
        :param verbose:
        :return:
        """
        local_class = TrailerDialog
        title = ''
        if movie is None:
            return ''
        try:
            title = movie.get(Movie.DETAIL_TITLE)
            if title is None:
                title = movie.get(Movie.TITLE)
                local_class.logger.error('Missing DETAIL_TITLE:',
                                         Debug.dump_dictionary(movie))
            title = '[B]' + title + '[/B]'

            if verbose:
                cached = False
                normalized = False
                if movie.get(Movie.NORMALIZED_TRAILER) is not None:
                    normalized = True
                elif movie.get(Movie.CACHED_TRAILER) is not None:
                    cached = True

                title += ' - ' + movie.get(Movie.DETAIL_CERTIFICATION, '')
                if normalized:
                    title = title + ' Normalized'
                elif cached:
                    title = title + ' Cached'
                else:
                    pass

        except Exception as e:
            local_class.logger.exception('')

        return title

    def bold(self, text):
        # type: (str) -> str
        """

        :return:
        """
        return '[B]' + text + '[/B]'

    def shutdown(self):
        # type: () -> None
        """
            Orderly stop execution of TrailerDialog.

            Note that this method can be called voluntarily, when the plugin
            decides to exit, as in the case of the configured number of trailers
            has played. OR, can be called by Monitor detecting an
            abort, in which case the shutdown still needs to be orderly, but
            since there are frequent checks for Monitor abort, the
            shutdown is less orderly, since the code is sprinkled with checks.
            In such case, some parts of the plugin can be shutting down already.

        :return:
        """
        local_class = TrailerDialog

        local_class.logger.enter()
        self._wait_or_interrupt_event.set()
        self.close()
        delete_player = False
        try:
            # if self.is_random_trailers_play_state() >=
            # DialogState.STARTED_PLAYING_MOVIE:
            delete_player = True

        except AbortException:
            delete_player = True
        finally:
            self._player_container.use_dummy_player(delete_player)

        self._title_control = None
        self._source = None
        self._movie = None
        self._viewed_playlist = None

