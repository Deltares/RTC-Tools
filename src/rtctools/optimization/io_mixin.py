import bisect
import logging
from abc import ABCMeta, abstractmethod

import casadi as ca

import numpy as np

from rtctools._internal.alias_tools import AliasDict
from rtctools._internal.caching import cached
from rtctools.optimization.optimization_problem import OptimizationProblem
from rtctools.optimization.timeseries import Timeseries

logger = logging.getLogger("rtctools")


class IOMixin(OptimizationProblem, metaclass=ABCMeta):
    """
    Base class for all IO methods of optimization problems.
    """

    def __init__(self, **kwargs):
        # Call parent class first for default behaviour.
        super().__init__(**kwargs)

        # Additional output variables
        self.__output_timeseries = set()

    def pre(self) -> None:
        # Call parent class first for default behaviour.
        super().pre()

        # Call read method to read all input
        self.read()

    @abstractmethod
    def read(self) -> None:
        """
        Reads input data from files
        """
        pass

    def post(self) -> None:
        # Call parent class first for default behaviour.
        super().post()

        # Call write method to write all output
        self.write()

    @abstractmethod
    def write(self) -> None:
        """"
        Writes output data to files
        """
        pass

    def times(self, variable=None) -> np.ndarray:
        """
        Returns the times in seconds from the forecast index and onwards

        :param variable:
        """
        return self.get_times()[self.get_forecast_index():]

    def get_timeseries(self, variable: str, ensemble_member: int = 0) -> Timeseries:
        return Timeseries(self.get_times(), self.get_timeseries_values(variable, ensemble_member))

    def set_timeseries(
            self,
            variable: str,
            timeseries: Timeseries,
            ensemble_member: int = 0,
            output: bool = True,
            check_consistency: bool = True):

        def stretch_values(values, t_pos):
            # Construct a values range with preceding and possibly following nans
            new_values = np.full_like(self.__timeseries_times_sec, np.nan)
            new_values[t_pos:] = values
            return new_values

        if output:
            self.__output_timeseries.add(variable)

        if isinstance(timeseries, Timeseries):
            if len(timeseries.values) != len(timeseries.times):
                raise ValueError('IOMixin: Trying to set timeseries {} with times and values that are of '
                                 'different length (lengths of {} and {}, respectively).'
                                 .format(variable, len(timeseries.times), len(timeseries.values)))

            if not np.array_equal(self.__timeseries_times_sec, timeseries.times):
                if check_consistency:
                    raise ValueError(
                        'IOMixin: Trying to set timeseries {} with different times '
                        '(in seconds) than the imported timeseries. Please make sure the '
                        'timeseries covers all timesteps of the longest '
                        'imported timeseries.'.format(variable)
                    )

                # Determine position of first times of added timeseries within the
                # import times. For this we assume that both time ranges are ordered,
                # and that the times of the added series is a subset of the import
                # times.
                t_pos = bisect.bisect_left(self.__timeseries_times_sec, timeseries.times[0])

                # Construct a new values range with length of self.__timeseries_times_sec
                values = stretch_values(timeseries.values, t_pos)
            else:
                values = timeseries.values

        else:
            if len(self.times()) == len(timeseries):
                values = timeseries
            else:
                if check_consistency:
                    raise ValueError('IOMixin: Trying to set values for {} with a different '
                                     'length ({}) than the forecast length. Please make sure the '
                                     'values covers all timesteps of the longest imported timeseries (length {}).'
                                     .format(variable, len(timeseries), len(self.times())))

                # If times is not supplied with the timeseries, we add the
                # forecast times range to a new Timeseries object. Hereby
                # we assume that the supplied values stretch from T0 to end.
                t_pos = self.get_forecast_index()

                # Construct a new values range with length of self.__timeseries_times_sec
                values = stretch_values(timeseries, t_pos)

        self.set_timeseries_values(variable, values, ensemble_member)

    def min_timeseries_id(self, variable: str) -> str:
        """
        Returns the name of the lower bound timeseries for the specified variable.

        :param variable: Variable name.
        """
        return '_'.join((variable, 'Min'))

    def max_timeseries_id(self, variable: str) -> str:
        """
        Returns the name of the upper bound timeseries for the specified variable.

        :param variable: Variable name.
        """
        return '_'.join((variable, 'Max'))

    @cached
    def bounds(self):
        # Call parent class first for default values.
        bounds = super().bounds()

        forecast_index = self.get_forecast_index()

        # Load bounds from timeseries
        for variable in self.dae_variables['free_variables']:
            variable_name = variable.name()

            m, M = None, None

            timeseries_id = self.min_timeseries_id(variable_name)
            try:
                m = self.get_timeseries_values(timeseries_id, 0)[forecast_index:]
            except KeyError:
                pass
            else:
                if logger.getEffectiveLevel() == logging.DEBUG:
                    logger.debug("Read lower bound for variable {}".format(variable_name))

            timeseries_id = self.max_timeseries_id(variable_name)
            try:
                M = self.get_timeseries_values(timeseries_id, 0)[forecast_index:]
            except KeyError:
                pass
            else:
                if logger.getEffectiveLevel() == logging.DEBUG:
                    logger.debug("Read upper bound for variable {}".format(variable_name))

            # Replace NaN with +/- inf, and create Timeseries objects
            if m is not None:
                m[np.isnan(m)] = np.finfo(m.dtype).min
                m = Timeseries(self.get_times()[forecast_index:], m)
            if M is not None:
                M[np.isnan(M)] = np.finfo(M.dtype).max
                M = Timeseries(self.get_times()[forecast_index:], M)

            # Store
            if m is not None or M is not None:
                bounds[variable_name] = (m, M)
        return bounds

    @cached
    def history(self, ensemble_member):
        # Load history
        history = AliasDict(self.alias_relation)

        end_index = self.get_forecast_index() + 1
        variable_list = self.dae_variables['states'] + self.dae_variables['algebraics'] + \
            self.dae_variables['control_inputs'] + self.dae_variables['constant_inputs']

        for variable in variable_list:
            variable = variable.name()
            try:
                history[variable] = Timeseries(
                    self.get_times()[:end_index],
                    self.get_timeseries_values(variable, ensemble_member)[:end_index])
            except KeyError:
                pass
            else:
                if logger.getEffectiveLevel() == logging.DEBUG:
                    logger.debug("IOMixin: Read history for state {}".format(variable))
        return history

    @cached
    def seed(self, ensemble_member):
        # Call parent class first for default values.
        seed = super().seed(ensemble_member)

        # Load seeds
        for variable in self.dae_variables['free_variables']:
            variable = variable.name()
            try:
                s = Timeseries(
                    self.get_times(),
                    self.get_timeseries_values(variable, ensemble_member)
                )
            except KeyError:
                pass
            else:
                if logger.getEffectiveLevel() == logging.DEBUG:
                    logger.debug("IOMixin: Seeded free variable {}".format(variable))
                # A seeding of NaN means no seeding
                s.values[np.isnan(s.values)] = 0.0
                seed[variable] = s
        return seed

    @cached
    def constant_inputs(self, ensemble_member):
        # Call parent class first for default values.
        constant_inputs = super().constant_inputs(ensemble_member)

        # Load inputs from timeseries
        for variable in self.dae_variables['constant_inputs']:
            variable = variable.name()
            try:
                timeseries = Timeseries(
                    self.get_times(),
                    self.get_timeseries_values(variable, ensemble_member)
                )
            except KeyError:
                pass
            else:
                if np.any(np.isnan(timeseries.values[self.get_forecast_index():])):
                    raise Exception("IOMixin: Constant input {} contains NaN".format(variable))
                constant_inputs[variable] = timeseries
                if logger.getEffectiveLevel() == logging.DEBUG:
                    logger.debug("IOMixin: Read constant input {}".format(variable))
        return constant_inputs

    def timeseries_at(self, variable, t, ensemble_member=0):
        return self.interpolate(t, self.get_times(), self.get_timeseries_values(variable, ensemble_member))

    @property
    def output_variables(self):
        variables = super().output_variables
        variables.extend([ca.MX.sym(variable) for variable in self.__output_timeseries])
        return variables