# This file is part of ts_ATDomeTrajectory.
#
# Developed for the LSST Telescope and Site Systems.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import asyncio
import math
import os
import pathlib
import unittest

import asynctest
import astropy.units as u
import yaml

from lsst.ts import salobj
from lsst.ts import ATDomeTrajectory

STD_TIMEOUT = 2  # standard command timeout (sec)
LONG_TIMEOUT = 20  # time limit for starting a SAL component (sec)
TEST_CONFIG_DIR = pathlib.Path(__file__).parents[1].joinpath("tests", "data", "config")

RAD_PER_DEG = math.pi / 180


class ATDomeTrajectoryTestCase(salobj.BaseCscTestCase, asynctest.TestCase):
    def setUp(self):
        self.dome_csc = None
        self.dome_remote = None
        self.atmcs_controller = None

    async def tearDown(self):
        for item_to_close in (self.dome_csc, self.dome_remote, self.atmcs_controller):
            if item_to_close is not None:
                await item_to_close.close()

    def basic_make_csc(self, initial_state, config_dir, simulation_mode):
        self.dome_csc = ATDomeTrajectory.FakeATDome(initial_state=salobj.State.ENABLED)
        self.dome_remote = salobj.Remote(domain=self.dome_csc.domain, name="ATDome")
        self.atmcs_controller = salobj.Controller("ATMCS")
        return ATDomeTrajectory.ATDomeTrajectory(
            initial_state=initial_state,
            config_dir=config_dir,
            simulation_mode=simulation_mode,
        )

    async def test_bin_script(self):
        """Test that run_atdometrajectory.py runs the CSC.
        """
        await self.check_bin_script(
            name="ATDomeTrajectory", index=None, exe_name="run_atdometrajectory.py",
        )

    async def test_standard_state_transitions(self):
        """Test standard CSC state transitions.
        """
        async with self.make_csc(initial_state=salobj.State.STANDBY):
            await self.check_standard_state_transitions(enabled_commands=())

    async def test_simple_follow(self):
        """Test that dome follows telescope using the "simple" algorithm.
        """
        async with self.make_csc(initial_state=salobj.State.ENABLED):

            az_cmd_state = await self.dome_remote.evt_azimuthCommandedState.next(
                flush=False, timeout=STD_TIMEOUT
            )
            self.assertEqual(az_cmd_state.commandedState, 1)  # 1=Unknown

            alt_deg = 40
            min_daz_to_move = self.csc.algorithm.max_daz.deg / math.cos(
                alt_deg * RAD_PER_DEG
            )
            for az_deg in (min_daz_to_move + 0.001, 180, -0.001):
                with self.subTest(az_deg=az_deg):
                    await self.check_move(az_deg, alt_deg=alt_deg)

            await self.check_null_moves(alt_deg=alt_deg)

    async def test_default_config_dir(self):
        async with self.make_csc(initial_state=salobj.State.STANDBY):
            desired_config_pkg_name = "ts_config_attcs"
            desired_config_env_name = desired_config_pkg_name.upper() + "_DIR"
            desird_config_pkg_dir = os.environ[desired_config_env_name]
            desired_config_dir = (
                pathlib.Path(desird_config_pkg_dir) / "ATDomeTrajectory/v1"
            )
            self.assertEqual(self.csc.get_config_pkg(), desired_config_pkg_name)
            self.assertEqual(self.csc.config_dir, desired_config_dir)
            await self.csc.do_exitControl(data=None)
            await asyncio.wait_for(self.csc.done_task, timeout=5)

    async def test_configuration(self):
        async with self.make_csc(
            initial_state=salobj.State.STANDBY, config_dir=TEST_CONFIG_DIR
        ):
            self.assertEqual(self.csc.summary_state, salobj.State.STANDBY)
            state = await self.remote.evt_summaryState.next(
                flush=False, timeout=LONG_TIMEOUT
            )
            self.assertEqual(state.summaryState, salobj.State.STANDBY)

            for bad_config_name in (
                "no_such_file.yaml",
                "invalid_no_such_algorithm.yaml",
                "invalid_malformed.yaml",
                "invalid_bad_max_daz.yaml",
            ):
                with self.subTest(bad_config_name=bad_config_name):
                    self.remote.cmd_start.set(settingsToApply=bad_config_name)
                    with salobj.assertRaisesAckError():
                        await self.remote.cmd_start.start(timeout=STD_TIMEOUT)

            self.remote.cmd_start.set(settingsToApply="valid.yaml")
            await self.remote.cmd_start.start(timeout=STD_TIMEOUT)
            self.assertEqual(self.csc.summary_state, salobj.State.DISABLED)
            state = await self.remote.evt_summaryState.next(
                flush=False, timeout=STD_TIMEOUT
            )
            self.assertEqual(state.summaryState, salobj.State.DISABLED)
            settings = await self.remote.evt_algorithm.next(
                flush=False, timeout=STD_TIMEOUT
            )
            self.assertEqual(settings.algorithmName, "simple")
            # max_daz is hard coded in the yaml file
            self.assertEqual(
                yaml.safe_load(settings.algorithmConfig), dict(max_daz=7.1)
            )

    async def assert_dome_az(self, expected_az, move_expected):
        """Check the ATDome and ATDomeController commanded azimuth.
        """
        if move_expected:
            az_cmd_state = await self.dome_remote.evt_azimuthCommandedState.next(
                flush=False, timeout=STD_TIMEOUT
            )
            az_cmd_state = self.dome_remote.evt_azimuthCommandedState.get()
            self.assertEqual(az_cmd_state.commandedState, 2)  # 1=GoToPosition
            salobj.assertAnglesAlmostEqual(az_cmd_state.azimuth, expected_az)
        else:
            with self.assertRaises(asyncio.TimeoutError):
                await self.dome_remote.evt_azimuthCommandedState.next(
                    flush=False, timeout=0.2
                )

    def assert_target_azalt(self, expected_az, expected_alt):
        salobj.assertAnglesAlmostEqual(self.csc.target_azalt.az, expected_az)
        salobj.assertAnglesAlmostEqual(self.csc.target_azalt.alt, expected_alt)

    async def check_move(self, az_deg, alt_deg):
        """Set telescope target azimuth and check that the dome goes there.

        Then check that the dome does not move for small changes
        to the telescope target about that point.

        Parameters
        ----------
        az_deg : `float`
            Desired azimuth for telescope and dome (deg)
        alt_deg : `float`
            Desired altitude for telescope (deg)

        Raises
        ------
        ValueError :
            If the change in dome azimuth <= configured max dome azimuth error
            (since that will result in no dome motion, which will mess up
            the test).
        """
        max_daz_deg = self.csc.algorithm.max_daz.deg
        scaled_daz_deg = (az_deg - self.dome_csc.cmd_az.deg) * math.cos(
            alt_deg * RAD_PER_DEG
        )
        if abs(scaled_daz_deg) <= max_daz_deg:
            raise ValueError(
                f"scaled_daz_deg={scaled_daz_deg} must be > max_daz_deg={max_daz_deg}"
            )

        self.atmcs_controller.evt_target.set_put(
            elevation=alt_deg, azimuth=az_deg, force_output=True
        )
        await self.assert_dome_az(az_deg, move_expected=True)
        self.assert_target_azalt(az_deg, alt_deg)
        await asyncio.wait_for(self.wait_dome_move(az_deg), timeout=LONG_TIMEOUT)
        await self.check_null_moves(alt_deg)

    async def wait_dome_move(self, az_deg):
        """Wait for an ATDome azimuth move to finish.

        Parameters
        ----------
        az_deg : `float`
            Target azimuth for telescope and dome (deg)
        """
        while True:
            curr_pos = await self.dome_remote.tel_position.next(
                flush=True, timeout=STD_TIMEOUT
            )
            if salobj.angle_diff(curr_pos.azimuthPosition, az_deg) < 0.1 * u.deg:
                break

    async def check_null_moves(self, alt_deg):
        """Check that small telescope moves do not trigger dome motion.

        Parameters
        ----------
        alt_deg : `float`
            Target altitude for telescope (deg)
        """
        az_deg = self.dome_csc.cmd_az.deg
        max_daz_deg = self.csc.algorithm.max_daz.deg
        no_move_daz_deg = (max_daz_deg - 0.0001) * math.cos(alt_deg * RAD_PER_DEG)
        for target_az_deg in (
            az_deg - no_move_daz_deg,
            az_deg + no_move_daz_deg,
            az_deg,
        ):
            self.atmcs_controller.evt_target.set_put(
                elevation=alt_deg, azimuth=target_az_deg, force_output=True
            )
            await self.assert_dome_az(az_deg, move_expected=False)
            self.assert_target_azalt(target_az_deg, alt_deg)


if __name__ == "__main__":
    unittest.main()