# -*- coding: utf-8 -*-
"""
Created on Apr 14, 2019

@author: Frank Feuerbacher
"""
from __future__ import absolute_import, division, print_function, unicode_literals

from common.imports import *

import sys
import datetime
import time
import threading
import six

from common.constants import Constants, Movie
from common.disk_utils import DiskUtils
from common.debug_utils import Debug
from common.exceptions import AbortException
from common.monitor import Monitor
from common.logger import (Logger, Trace, LazyLogger)
from common.settings import Settings
from common.development_tools import (Any, Callable, Optional, List,
                                      Union,
                                      TextType, RESOURCE_LIB,
                                      resource)
from discovery.restart_discovery_exception import RestartDiscoveryException
from backend.rating import Rating
from backend.genreutils import GenreUtils
from backend.movie_utils import LibraryMovieStats
from backend.json_utils_basic import JsonUtilsBasic
from backend.json_utils import JsonUtils
from discovery.base_discover_movies import BaseDiscoverMovies
from discovery.library_movie_data import (LibraryMovieData, LibraryNoTrailerMovieData,
                                          LibraryURLMovieData)

if Constants.INCLUDE_MODULE_PATH_IN_LOGGER:
    module_logger = LazyLogger.get_addon_module_logger().getChild(
        'discovery.discover_library_movies')
else:
    module_logger = LazyLogger.get_addon_module_logger()


# noinspection Annotator
class DiscoverLibraryMovies(BaseDiscoverMovies):
    """
        Retrieve all movie entries from library. If specified, then limit to movies
        for the given genre. Note that entries include movies without trailers.
        Movies with local trailers or trailer URLs are immediately placed into
        BaseTrailerManager.readyToPlay. The others are placed into
        BaseTrailerManager.trailerFetchQue.
    """

    _singleton_instance = None

    def __init__(self,
                 group=None,  # type: None
                 # type: Callable[Union[None, Any], Union[Any, None]]
                 target=None,
                 thread_name=None,  # type: TextType
                 args=(),  # type: Optional[Any]
                 kwargs=None  # type: Optional[Any]
                 ):
        # type: (...) -> None
        """

        :param group:
        :param target:
        :param thread_name:
        :param args:
        :param kwargs:
        """
        self._logger = module_logger.getChild(self.__class__.__name__)
        thread_name = type(self).__name__
        if kwargs is None:
            kwargs = {}
        kwargs[Movie.SOURCE] = Movie.LIBRARY_SOURCE
        super().__init__(group=None, target=None, thread_name=thread_name,
                         args=(), kwargs=None)
        self._movie_data = LibraryMovieData()

        self._selected_genres = []
        self._selected_keywords = []
        self._excluded_genres = []
        self._excluded_keywords = []
        self._libraryURLManager = None
        self._libraryNoTrailerInfoManager = None
        self._some_movies_discovered_event = threading.Event()

    def discover_basic_information(self):
        # type: () -> None
        """

        :return:
        """

        self.start()
        #
        # In order to give good response during startup, block
        # other discovery (TMDB, iTunes) until a few local trailers have
        # been located (~50).

        self._some_movies_discovered_event.wait(timeout=15)

        if self._logger.isEnabledFor(Logger.DEBUG):
            self._logger.debug(': started')

    def on_settings_changed(self):
        # type: () -> None
        """
            Rediscover trailers if the changed settings impacts this manager.
        """
        self._logger.enter()

        try:
            if Settings.is_library_loading_settings_changed():
                stop_thread = not Settings.get_include_library_trailers()
                self.restart_discovery(stop_thread)
        except (Exception) as e:
            self._logger.exception('')

    def run(self):
        # type: () -> None
        """

        :return:
        """
        if RESOURCE_LIB:
            memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if self._logger.isEnabledFor(Logger.DEBUG):
                self._logger.debug(': memory:', memory)
        start_time = datetime.datetime.now()
        try:
            finished = False
            while not finished:
                try:
                    self._some_movies_discovered_event.clear()
                    self.run_worker()
                    self.wait_until_restart_or_shutdown()
                except (RestartDiscoveryException):
                    # Restart discovery
                    if self._logger.isEnabledFor(Logger.DEBUG):
                        self._logger.debug('Restarting discovery')
                    self.prepare_for_restart_discovery()
                    if not Settings.get_include_library_trailers():
                        finished = True
                        self.remove_self()
                self._some_movies_discovered_event.set()

        except AbortException:
            return  # Just exit thread
        except Exception as e:
            self._logger.exception('')

        self.finished_discovery()

        # Unblock other discovery threads

        self._some_movies_discovered_event.set()

        duration = datetime.datetime.now() - start_time
        if self._logger.isEnabledFor(Logger.DEBUG):
            self._logger.debug('Time to discover:', duration.seconds, 'seconds',
                               trace=Trace.STATS)

    def create_query(self,
                     included_genres,  # type: List[TextType]
                     excluded_genres,  # type: List[TextType]
                     included_tags,  # type: List[TextType]
                     excluded_tags  # type: List[TextType]
                     ):
        # type: (...) -> TextType
        """

        :param included_genres:
        :param excluded_genres:
        :param included_tags:
        :param excluded_tags:
        :return:
        """

        formatted_genre_list = ['"%s"' % genre for genre in included_genres]
        formatted_genre_list = ', '.join(formatted_genre_list)

        formatted_excluded_genre_list = ['"%s"' %
                                         genre for genre in excluded_genres]
        formatted_excluded_genre_list = ', '.join(
            formatted_excluded_genre_list)

        formatted_tag_list = ['"%s"' % tag for tag in included_tags]
        formatted_tag_list = ', '.join(formatted_tag_list)

        formatted_excluded_tag_list = ['"%s"' % tag for tag in excluded_tags]
        formatted_excluded_tag_list = ', '.join(formatted_excluded_tag_list)

        query_prefix = '{"jsonrpc": "2.0", "method": "VideoLibrary.GetMovies", \
                    "params": {\
                    "properties": \
                        ["title", "lastplayed", "studio", "cast", "plot", "writer", \
                        "director", "fanart", "runtime", "mpaa", "thumbnail", "file", \
                        "year", "genre", "tag", "trailer", "uniqueid"]'
        query_suffix = '}, "id": 1}'
        query_filter_prefix = ''
        include_query_filter = ''
        tag_filter = ''
        genre_filter = ''

        if (len(included_genres) > 0 or len(included_tags) > 0 or len(excluded_genres) > 0
                or len(excluded_tags) > 0):
            query_filter_prefix = ', "filter": '

        exclude_filters = []
        include_filters = []
        if len(included_genres) > 0:
            genre_filter = ('{"field": "genre", "operator": "contains", "value": [%s]}'
                            % formatted_genre_list)
            include_filters.append(genre_filter)

        if len(included_tags) > 0:
            tag_filter = ('{"field": "tag", "operator": "contains", "value": [%s]}' %
                          formatted_tag_list)
            include_filters.append(tag_filter)

        combined_include_filter = []
        include_sub_query_filter = ''
        if len(include_filters) > 1:
            include_sub_query_filter = '{"or": [%s]}' % ', '.join(
                include_filters)
            combined_include_filter.append(include_sub_query_filter)
        elif len(include_filters) == 1:
            combined_include_filter.append(include_filters[0])

        if len(excluded_genres) > 0:
            excluded_genre_filter = ('{"field": "genre", "operator": "doesnotcontain", "value": [%s]}'
                                     % formatted_excluded_genre_list)
            exclude_filters.append(excluded_genre_filter)

        if len(excluded_tags) > 0:
            excluded_tag_filter = ('{"field": "tag", "operator": "doesnotcontain", "value": [%s]}' %
                                   formatted_excluded_tag_list)
            exclude_filters.append(excluded_tag_filter)

        combined_exclude_filter = []
        exclude_sub_query_filter = ''
        if len(exclude_filters) > 1:
            exclude_sub_query_filter = '{"or": [%s]}' % ', '.join(
                exclude_filters)
        elif len(exclude_filters) == 1:
            exclude_sub_query_filter = exclude_filters[0]

        combined_exclude_filter.append(exclude_sub_query_filter)

        combined_filter = []
        if len(combined_include_filter) > 0:
            combined_filter.append(combined_include_filter[0])
        if len(combined_exclude_filter) > 0:
            combined_filter.append(combined_exclude_filter[0])
        query_filter = ''
        if len(combined_filter) > 1:
            query_filter = '{"and": [%s]}' % ', '.join(combined_filter)
        elif len(combined_filter) == 1:
            query_filter = combined_filter[0]

        query = (query_prefix + query_filter_prefix +
                 query_filter + query_suffix)

        if self._logger.isEnabledFor(Logger.DEBUG):
            self._logger.debug('query', 'genres:', included_genres,
                               'excluded_genres:', excluded_genres, 'tags:',
                               included_tags, 'excluded_tags:',
                               excluded_tags, query)

        return query

    def run_worker(self):
        # type: () -> None
        """
           Initial Discovery of all movies in Kodi.

        :return:
        """
        # Discovery is done in two parts:
        #
        # 1- query DB for every movie in library
        # 2- Get additional information
        #
        # There are three types of trailers for these movies:
        #
        #  a- Movies with local trailers
        #  b- Movies with trailer URLS (typically youtube links from tmdb)
        #    TMdb will need to be queried for details
        #  c. Movies with no trailer information, requiring a check with tmdb
        #     to see if one exists
        #
        # Because of the above, this manager will query the DB for every movie
        # and then only process the ones with local trailers. The others will
        # be handed off to their own managers. This is done because of
        # the way that this application works:
        #    Once enough information to identify a movie that matches
        #    what the user wants, it is added to the pool of movies that
        #    can be randomly selected for playing. Once a movie has been
        #    selected, it is placed into a TrailerFetcherQueue. A
        #    TrailerFetcher then gathers the remaining information so that
        #    it can be played.
        #
        #    If the lion's share of movies in the pool require significant
        #    extra processing because they don't have local trailers, then
        #    the fetcher can get overwhelmed.

        self._selected_keywords = []
        self._excluded_keywords = []
        self._selected_genres = []
        self._excluded_genres = []
        if Settings.get_filter_genres():
            self._selected_genres = GenreUtils.get_instance().get_internal_kodi_genre_ids(
                GenreUtils.LOCAL_DATABASE, exclude=False)
            self._excluded_genres = GenreUtils.get_instance().get_internal_kodi_genre_ids(
                GenreUtils.LOCAL_DATABASE, exclude=True)
            self._selected_keywords = GenreUtils.get_instance().get_internal_kodi_keyword_ids(
                GenreUtils.LOCAL_DATABASE, exclude=False)
            self._excluded_keywords = GenreUtils.get_instance().get_internal_kodi_keyword_ids(
                GenreUtils.LOCAL_DATABASE, exclude=True)

        query = self.create_query(
            self._selected_genres, self._excluded_genres, self._selected_keywords,
            self._excluded_keywords)

        if Monitor.is_abort_requested():
            return

        start_time = datetime.datetime.now()
        query_result = JsonUtilsBasic.get_kodi_json(query, dump_results=False)
        elapsed_time = datetime.datetime.now() - start_time
        self._logger.debug('Library query seconds:',
                           elapsed_time.total_seconds())

        movies_skipped = 0
        movies_found = 0
        movies_with_local_trailers = 0
        movies_with_trailer_urls = 0
        movies_without_trailer_info = 0

        self.throw_exception_on_forced_to_stop()
        result = query_result.get('result', {})
        del query_result
        movies = result.get('movies', [])
        del result

        DiskUtils.RandomGenerator.shuffle(movies)
        if self._libraryURLManager is None:
            self._libraryURLManager = DiscoverLibraryURLTrailerMovies()
            self._libraryNoTrailerInfoManager = DiscoverLibraryNoTrailerMovies()
        library_movies = []
        library_url_movies = []
        library_no_trailer_movies = []
        empty_limit = 50
        movie_data = None
        if Settings.is_enable_movie_stats():
            movie_data = LibraryMovieStats()

        for movie in movies:
            self.throw_exception_on_forced_to_stop()
            try:
                movies_found += 1
                if Settings.get_hide_watched_movies() and Movie.LAST_PLAYED in movie:
                    if (self.get_days_since_last_played(movie[Movie.LAST_PLAYED],
                                                        movie[Movie.TITLE]) >
                            Settings.get_minimum_days_since_watched()):
                        movies_skipped += 1
                        if self._logger.isEnabledFor(Logger.DEBUG):
                            self._logger.debug(movie[Movie.TITLE],
                                               'will not be played due to Hide',
                                               'Watched Movies')
                        continue

                # Normalize rating

                if self._logger.isEnabledFor(Logger.DEBUG):
                    self._logger.debug(': mpaa:', movie[Movie.MPAA],
                                       'movie:', movie[Movie.TITLE])
                rating = Rating.get_mpa_rating(
                    movie.get(Movie.MPAA), movie.get('adult'))
                movie[Movie.ADULT] = movie.get('adult')
                movie[Movie.SOURCE] = Movie.LIBRARY_SOURCE
                movie.setdefault(Movie.TRAILER, '')
                movie[Movie.TYPE] = ''

                if self._logger.isEnabledFor(Logger.DEBUG):
                    Debug.validate_basic_movie_properties(movie)

                if Settings.is_enable_movie_stats():
                    movie_data.collect_data(movie)

                # Basic discovery is complete at this point. Now send
                # all of the movies without any trailer information to
                # DiscoverLibraryNoTrailerMovies while
                # those with trailer URLs to DiscoverLibraryURLTrailerMovies

                if Rating.check_rating(rating):
                    trailer = movie[Movie.TRAILER]
                    if trailer == '':
                        movies_without_trailer_info += 1
                        library_no_trailer_movies.append(movie)
                    elif trailer.startswith('plugin://') or trailer.startswith('http'):
                        movies_with_trailer_urls += 1
                        library_url_movies.append(movie)
                    elif Settings.get_include_library_trailers():
                        movies_with_local_trailers += 1
                        library_movies.append(movie)

                if len(library_movies) >= empty_limit:
                    self.add_to_discovered_trailers(library_movies)
                    del library_movies[:]

                    # Unblock other discovery now that a few movies have been
                    # found.

                    if not self._some_movies_discovered_event.isSet():
                        self._some_movies_discovered_event.set()

                if len(library_no_trailer_movies) >= empty_limit:
                    self._libraryNoTrailerInfoManager.add_to_discovered_trailers(
                        library_no_trailer_movies)
                    del library_no_trailer_movies[:]
                if len(library_url_movies) >= empty_limit:
                    self._libraryURLManager.add_to_discovered_trailers(
                        library_url_movies)
                    del library_no_trailer_movies[:]

                    # Unblock other discovery now that a few movies have been
                    # found.

                    self._some_movies_discovered_event.set()

            except AbortException:
                six.reraise(*sys.exc_info())
            except Exception:
                self._logger.exception('')
        try:
            if len(library_movies) >= 0:
                self.add_to_discovered_trailers(library_movies)
            if len(library_no_trailer_movies) >= 0:
                self._libraryNoTrailerInfoManager.add_to_discovered_trailers(
                    library_no_trailer_movies)
            if len(library_url_movies) >= 0:
                self._libraryURLManager.add_to_discovered_trailers(
                    library_url_movies)

        except AbortException:
            six.reraise(*sys.exc_info())
        except Exception:
            self._logger.exception('')

        if self._logger.isEnabledFor(Logger.DEBUG):
            self._logger.debug('Local movies found in library:',
                               movies_found, trace=Trace.STATS)
            self._logger.debug('Local movies filtered out',
                               movies_skipped, trace=Trace.STATS)
            self._logger.debug('Movies with local trailers:',
                               movies_with_local_trailers, trace=Trace.STATS)
            self._logger.debug('Movies with trailer URLs:',
                               movies_with_trailer_urls, trace=Trace.STATS)
            self._logger.debug('Movies with no trailer information:',
                               movies_without_trailer_info, trace=Trace.STATS)

        if Settings.is_enable_movie_stats():
            self._logger('Starting to report Movie Stats')
            movie_data.report_data()
            del movie_data
            self._logger('Finished reporting Movie Stats')

# noinspection Annotator,PyArgumentList


class DiscoverLibraryURLTrailerMovies(BaseDiscoverMovies):
    """
        This manager does not do any discovery, it receives local movies
        with trailer URLs from LibraryManager. This manager primarily
        acts as a container to hold the list of movies while the
        TrailerFetcher and BaseTrailerManager does the work
    """

    def __init__(self):
        # type: () -> None
        """

        """
        self._logger = module_logger.getChild(self.__class__.__name__)
        thread_name = type(self).__name__
        kwargs = {}
        kwargs[Movie.SOURCE] = Movie.LIBRARY_URL_TRAILER
        super().__init__(group=None, target=None, thread_name=thread_name,
                         args=(), kwargs=None)
        self._movie_data = LibraryURLMovieData()

    def on_settings_changed(self):
        # type: () -> None
        """
            Settings changes is handled by DiscoveryLibraryTrailerMovies

            Settings changes only impact library entries with Trailer URLs
            to stop it. Since we are here, Trailer URL discovery was active
            prior to the settings change, therefore, only do something if
            we are no longer active.
        """
        self._logger.enter()

        try:
            stop_thread = not Settings.get_include_library_remote_trailers()
            if stop_thread:
                self.restart_discovery(stop_thread)
        except AbortException:
            pass  # don't pass exception to handler
        except Exception as e:
            self._logger.exception('')

    def discover_basic_information(self):
        # type: () -> None
        """

        :return:
        """

        if self._logger.isEnabledFor(Logger.DEBUG):
            self._logger.debug(' dummy method')

    def run(self):
        # type: () -> None
        """

        :return:
        """
        if self._logger.isEnabledFor(Logger.DEBUG):
            self._logger.debug(' dummy thread, Join Me!')
        finished = False
        while not finished:
            try:
                self.finished_discovery()
                self.wait_until_restart_or_shutdown()
            except (RestartDiscoveryException):
                # Restart discovery
                self._logger.debug('Restarting discovery')
                self.prepare_for_restart_discovery()
            except AbortException:
                return  # Just exit thread
            except Exception as e:
                self._logger.exception('')


# noinspection Annotator
class DiscoverLibraryNoTrailerMovies(BaseDiscoverMovies):
    """
        This manager does not do any discovery, it receives local movies
        without any trailer information from LibraryManager. This manager
        primarily acts as a container to hold the list of movies while the
        TrailerFetcher and BaseTrailerManager does the work
    """

    def __init__(self):
        # type: () -> None
        """

        """
        self._logger = module_logger.getChild(self.__class__.__name__)
        thread_name = type(self).__name__
        self._validate_number_of_trailers = 0
        self._reported_trailers = 0
        kwargs = {}
        kwargs[Movie.SOURCE] = Movie.LIBRARY_NO_TRAILER
        super().__init__(group=None, target=None, thread_name=thread_name,
                         args=(), kwargs=None)
        self._movie_data = LibraryNoTrailerMovieData()

    def on_settings_changed(self):
        # type: () -> None
        """
            Settings changes is handled by DiscoveryLibraryTrailerMovies

            Settings changes only impact discovery of library entries without
            Trailer entries to stop it. Since we are here, Trailer discovery
            was active prior to the settings change, therefore, only do
            something if we are no longer active.
        """
        self._logger.enter()

        try:
            stop_thread = not Settings.get_include_library_no_trailer_info()
            if stop_thread:
                self.restart_discovery(stop_thread)
        except (Exception) as e:
            self._logger.exception('')

    def discover_basic_information(self):
        # type: () -> None
        """

        :return:
        """
        if self._logger.isEnabledFor(Logger.DEBUG):
            self._logger.debug(' dummy method')

    def run(self):
        # type: () -> None
        """

        :return:
        """
        if self._logger.isEnabledFor(Logger.DEBUG):
            self._logger.debug(' dummy thread, Join Me!')
        finished = False
        while not finished:
            try:
                self.finished_discovery()
                self.wait_until_restart_or_shutdown()
            except RestartDiscoveryException:
                # Restart discovery
                if self._logger.isEnabledFor(Logger.DEBUG):
                    self._logger.debug('Restarting discovery')
                self.prepare_for_restart_discovery()
            except AbortException:
                return  # Just exit thread
            except Exception as e:
                self._logger.exception('')

    def get_days_since_last_played(self, last_played_field, movie_name):
        # type: (TextType, TextType) -> int
        """
            Get the number of days since this movie (not the trailer)
            was last played. For invalid or missing values, -1 will be
            returned.
        """

        days_since_played = int(-1)
        try:
            if last_played_field is not None and last_played_field != '':
                pd = time.strptime(last_played_field, '%Y-%m-%d %H:%M:%S')
                pd = time.mktime(pd)
                pd = datetime.datetime.fromtimestamp(pd)
                last_play = datetime.datetime.now() - pd
                days_since_played = last_play.days
        except (AbortException, ShutdownException):
            six.reraise(*sys.exc_info())
        except (Exception) as e:
            if self._logger.isEnabledFor(Logger.DEBUG):
                self._logger.debug('Invalid lastPlayed field for', movie_name,
                                   ':', last_played_field)
            self._logger.exception('')
            raise e
        return days_since_played
