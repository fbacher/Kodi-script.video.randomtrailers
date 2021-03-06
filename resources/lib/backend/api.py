# -*- coding: utf-8 -*-

"""
Created on Feb 10, 2019

@author: fbacher
"""
from common.imports import *
from common.logger import (Logger, LazyLogger, Trace)
from common.constants import Constants, Movie
from common.settings import Settings
from common.monitor import Monitor
from backend.movie_utils import LibraryMovieStats
from discovery.base_discover_movies import BaseDiscoverMovies
from discovery.discover_library_movies import DiscoverLibraryMovies
from discovery.discover_folder_trailers import DiscoverFolderTrailers
from discovery.discover_itunes_movies import DiscoverItunesMovies
from discovery.discover_tmdb_movies import DiscoverTmdbMovies
from discovery.discover_tfh_movies import DiscoverTFHMovies

module_logger = LazyLogger.get_addon_module_logger(file_path=__file__)


def load_trailers():
    # type: () ->None
    """
        Start up the configured trailer discovery threads.

        Called whenever settings have changed to start any threads
        that have just ben enabled.

    :return:
    """

    module_logger.enter()

    if Settings.get_include_library_trailers():
        lib_instance = DiscoverLibraryMovies()
        lib_instance.discover_basic_information()

    # Manufacture trailer entries for folders which contain trailer
    # files. Note that files are assumed to be videos.
    if Settings.get_include_trailer_folders():
        DiscoverFolderTrailers().discover_basic_information()

    if Settings.get_include_itunes_trailers():
        DiscoverItunesMovies().discover_basic_information()

    if Settings.get_include_tmdb_trailers():
        DiscoverTmdbMovies().discover_basic_information()

    if Settings.is_include_tfh_trailers():
        DiscoverTFHMovies().discover_basic_information()

    Monitor.throw_exception_if_abort_requested(timeout=1.0)
    Monitor.set_startup_complete()


def get_genres_in_library():
    # type: () -> List[str]
    """

    :return:
    """
    return LibraryMovieStats.get_genres_in_library()
