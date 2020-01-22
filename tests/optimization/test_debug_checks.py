import logging
from datetime import datetime

import numpy as np

from rtctools.optimization.collocated_integrated_optimization_problem import (
    CollocatedIntegratedOptimizationProblem,
)
from rtctools.optimization.io_mixin import IOMixin
from rtctools.optimization.modelica_mixin import ModelicaMixin

from test_case import TestCase

from .data_path import data_path

logger = logging.getLogger("rtctools")


class Model(IOMixin, ModelicaMixin, CollocatedIntegratedOptimizationProblem):
    def __init__(self, **kwargs):
        kwargs["model_name"] = kwargs.get("model_name", "ModelDebugChecks")
        kwargs["input_folder"] = data_path()
        kwargs["output_folder"] = data_path()
        kwargs["model_folder"] = data_path()
        super().__init__(**kwargs)

    def compiler_options(self):
        compiler_options = super().compiler_options()
        compiler_options["cache"] = False
        compiler_options['library_folders'] = []
        return compiler_options

    def read(self):
        self.io.reference_datetime = datetime(2000, 1, 1)

        self.io.set_timeseries(
            'constant_input',
            [datetime(2000, 1, 1, 0, 0, 0), datetime(2000, 1, 1, 0, 0, 1)],
            np.array([1.0, 2.0]),
            0)

    def write(self):
        pass


class ModelMatrixCoeffLarge(Model):

    def path_objective(self, ensemble_member):
        return self.state('x')

    def path_constraints(self, ensemble_member):
        return [(1000.0 * self.state('x') - self.state('y'), 0.0, 0.0)]


class ModelMatrixCoeffSmall(ModelMatrixCoeffLarge):

    def path_constraints(self, ensemble_member):
        return [(0.001 * self.state('x') - self.state('y'), 0.0, 0.0)]


class ModelMatrixCoeffRowRange(ModelMatrixCoeffLarge):

    def path_constraints(self, ensemble_member):
        return [(0.01 * self.state('x') - 100.0 * self.state('y'), 0.0, 0.0)]


class ModelMatrixCoeffColRange(ModelMatrixCoeffLarge):

    def path_constraints(self, ensemble_member):
        return [(100.0 * self.state('x') - self.state('y'), 0.0, 0.0),
                (0.01 * self.state('x') - self.state('z'), 0.0, 0.0)]


class TestCheckMatrixCoefficients(TestCase):

    def _run_test(self, class_, message, assert_='assertIn', **kwargs):
        problem = class_()
        problem._debug_check_level = lambda level, name: name.endswith("__debug_check_transcribe_linear_coefficients")
        if kwargs:
            problem._debug_check_options = {
                "CollocatedIntegratedOptimizationProblem.__debug_check_transcribe_linear_coefficients": kwargs}

        with self.assertLogs('rtctools', level='INFO') as cm:
            problem.optimize()

        getattr(self, assert_)(message, cm.output)

    def test_matrix_coefficient_large(self):
        self._run_test(
            ModelMatrixCoeffLarge,
            "INFO:rtctools:Exceedence in jacobian of constraints evaluated at x0"
            " (max > 100, min < 0.01, or max / min > 1000):")

    def test_matrix_coefficient_small(self):
        self._run_test(
            ModelMatrixCoeffSmall,
            "INFO:rtctools:Exceedence in jacobian of constraints evaluated at x0"
            " (max > 100, min < 0.01, or max / min > 1000):")

    def test_matrix_coefficient_row_range(self):
        self._run_test(
            ModelMatrixCoeffRowRange,
            "INFO:rtctools:Exceedence in jacobian of constraints evaluated at x0"
            " (max > 100, min < 0.01, or max / min > 1000):")

    def test_matrix_coefficient_col_range(self):
        self._run_test(
            ModelMatrixCoeffColRange,
            "INFO:rtctools:Exceedence in range per column (max / min > 1000):")

    def test_matrix_coefficient_allow_larger_range(self):
        self._run_test(
            ModelMatrixCoeffRowRange,
            "INFO:rtctools:Exceedence in jacobian of constraints evaluated at x0"
            " (max > 0.01, min < 100, or max / min > 1000):",
            'assertNotIn',
            tol_range=1E4,
        )
