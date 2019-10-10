# -*- coding: utf-8 -*-

"""
Created on Feb 10, 2019

@author: fbacher
"""
from __future__ import absolute_import, division, print_function, unicode_literals

from common.imports import *

from common.development_tools import (
    Callable, List, TextType, MovieType)
from common.constants import (Constants, Movie)
from discovery.abstract_movie_data import AbstractMovieData


# noinspection Annotator,PyArgumentList
class TMDBMovieData(AbstractMovieData):
    """

    """

    def __init__(self, movie_source=''):
        # type: (TextType) -> None
        """

        """
        super().__init__(movie_source=Movie.TMDB_SOURCE)
        self.start_trailer_fetchers()

    def add_to_discovered_trailers(self, movies):
        # type: (Union[Dict[TextType], List[Dict[TextType]]]) -> None
        """

        :param movies:
        :return:
        """

        super().add_to_discovered_trailers(movies)

    def get_number_of_movies_with_trailers(self):
        # type: () -> int
        """

        :return:
        """
        movies_with_trailers = 0
        for movie in self._discovered_trailers:
            if movie[Movie.DISCOVERY_STATE] >= Movie.DISCOVERY_COMPLETE:
                movies_with_trailers += 1

        return movies_with_trailers
