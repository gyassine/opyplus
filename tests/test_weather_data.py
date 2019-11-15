import tempfile
import unittest
import os
import io

import pandas as pd

from oplus.configuration import CONF
from oplus import WeatherData
from oplus.compatibility import get_eplus_base_dir_path
from tests.util import assert_epw_equal, iter_eplus_versions  # todo: improve epw-equal and use it

from pandas.util.testing import assert_frame_equal

from tests.resources import Resources


class EPlusWeatherData(unittest.TestCase):
    # todo: [GL] make better checks
    def test_weather_series(self):
        # create weather data
        weather_data0 = WeatherData.load(Resources.Epw.san_fransisco_tmy3)

        # create new epw
        epw1 = weather_data0.save()
        weather_data1 = WeatherData.load(io.StringIO(epw1))

        # check
        assert_frame_equal(
            weather_data0.get_weather_series(),
            weather_data1.get_weather_series()
        )

    def test_datetime_index(self):
        with open(Resources.Epw.san_fransisco_tmy3) as f:
            sf_content = f.read()

        # create weather data and create datetime instants
        wd = WeatherData.load(io.StringIO(sf_content))
        wd.create_datetime_instants(start_year=2013)

        # generate epws with and without datetimes
        with_datetimes = wd.to_epw()
        without_datetimes = wd.to_epw(use_datetimes=False)

        # check coherence with initial
        sf_diff, other_diff = compare_sf(sf_content, without_datetimes)
        with open("sf_diff.txt", "w") as f:
            f.write(sf_diff)
        with open("other_diff.txt", "w") as f:
            f.write(other_diff)
        self.assertEqual(sf_diff, other_diff)
        # self.assertEqual(without_datetimes, initial_epw_content)


def compare_sf(sf_content, other_content):
    return _SfToEpwComparator(sf_content, other_content).diffs


class _SfToEpwComparator:
    def __init__(self, sf_content, other_content):
        self._sf_content = sf_content
        self._other_content = other_content
        self.sf_diff = ""
        self.other_diff = ""
        self._compare()

    @property
    def diffs(self):
        return self.sf_diff, self.other_diff

    def _compare(self, years_must_match=True):
        import time, math
        start = time.time()
        rows_to_skip = (
            1,  # SF differs from documentation, and we don't want to spend time on understanding
            2,  # not used for calculations
            5  # comment 1 is changed by oplus
        )
        for i, (sf_row, other_row) in enumerate(zip(io.StringIO(self._sf_content), io.StringIO(self._other_content))):
            # we skip if not interesting
            if i in rows_to_skip:
                continue
            # to speed up tests, we only test every 5 days + 1 hour (to make sure we have different days and hours)
            if (i >= 8) and (i % ((24*5)+1) != 0):
                continue

            # manage rows that we don't expect to be equal
            if i == 6:  # comments 2 (must strip)
                sf_row = ",".join([s.strip() for s in sf_row.split(",")])
            elif i == 7:  # data periods (useless Data field and must strip)
                sf_row = sf_row.replace("Data", "")
            elif i >= 8:
                sf_row_l = _normalize_data_row(sf_row)
                other_row_l = _normalize_data_row(other_row)

                if not years_must_match:
                    sf_row_l, other_row_l = sf_row_l[1:], other_row_l[1:]
                sf_row = ",".join(sf_row_l)
                other_row = ",".join(other_row_l)

            # strip newlines
            sf_row, other_row = sf_row.strip(), other_row.strip()

            # check diff and register if relevant
            if sf_row != other_row:
                self._register_difference(i, sf_row, other_row)
        print(time.time()-start)

    def _register_difference(self, row_num, sf_row, other_row):
        self.sf_diff += f"{row_num}: {sf_row}\n"
        self.other_diff += f"{row_num}: {other_row}\n"


def _normalize_data_row(row) -> list:
    return [str(float(v)) if j != 5 else v for (j, v) in enumerate(row.split(","))]