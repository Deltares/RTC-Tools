import datetime
import logging

import numpy as np

from rtctools.optimization.collocated_integrated_optimization_problem import (
    CollocatedIntegratedOptimizationProblem
)
from rtctools.optimization.csv_lookup_table_mixin import CSVLookupTableMixin
from rtctools.optimization.csv_mixin import CSVMixin
from rtctools.optimization.modelica_mixin import ModelicaMixin

from test_case import TestCase

from .data_path import data_path

logger = logging.getLogger("rtctools")
logger.setLevel(logging.WARNING)


class Model(CSVMixin, ModelicaMixin, CollocatedIntegratedOptimizationProblem):

    def __init__(self, **kwargs):
        kwargs["model_name"] = kwargs.get("model_name", "Model")
        kwargs["input_folder"] = data_path()
        kwargs["output_folder"] = data_path()
        kwargs["model_folder"] = data_path()
        super().__init__(**kwargs)

        self.csv_forecast_date = datetime.datetime(2013, 5, 19, 22)

    def objective(self, ensemble_member):
        # Quadratic penalty on state 'x' at final time
        xf = self.state_at("x", self.times()[-1])
        f = xf ** 2
        return f

    def constraints(self, ensemble_member):
        # No additional constraints
        return []

    def compiler_options(self):
        compiler_options = super().compiler_options()
        compiler_options["cache"] = False
        return compiler_options


class ModelLookup(CSVLookupTableMixin, Model):

    def __init__(self):
        super().__init__(
            input_folder=data_path(),
            output_folder=data_path(),
            model_name="Model",
            model_folder=data_path(),
            lookup_tables=["constant_input"],
        )


class ModelEnsemble(Model):

    csv_ensemble_mode = True

    def __init__(self):
        super().__init__(
            input_folder=data_path(),
            output_folder=data_path(),
            model_name="Model",
            model_folder=data_path(),
            lookup_tables=[],
        )


class TestCSVMixin(TestCase):

    def setUp(self):
        self.problem = Model()
        self.problem.optimize()
        self.results = self.problem.extract_results()
        self.tolerance = 1e-6

    def test_parameter(self):
        params = self.problem.parameters(0)
        self.assertEqual(params["k"], 1.01)

    def test_initial_state(self):
        initial_state = self.problem.initial_state(0)
        self.assertAlmostEqual(initial_state["x"], 1.02, self.tolerance)

    def test_history(self):
        history = self.problem.history(0)
        self.assertEqual(len(history['u'].times), 3)
        self.assertEqual(history['u'].times[1], -3600)
        self.assertAlmostEqual(history['u'].values[1], 0.2, self.tolerance)

    def test_objective_value(self):
        objective_value_tol = 1e-6
        self.assertTrue(abs(self.problem.objective_value) < objective_value_tol)

    def test_output(self):
        self.assertAlmostEqual(
            self.results["x"][:] ** 2 + np.sin(self.problem.times()),
            self.results["z"][:],
            self.tolerance,
        )

    def test_algebraic(self):
        self.assertAlmostEqual(
            self.results["y"] + self.results["x"],
            np.ones(len(self.problem.times())) * 3.0,
            self.tolerance,
        )

    def test_bounds(self):
        self.assertAlmostGreaterThan(self.results["u"], -2, self.tolerance)
        self.assertAlmostLessThan(self.results["u"], 2, self.tolerance)

    def test_interpolate(self):
        for v in ["x", "y", "u"]:
            for i in [0, int(len(self.problem.times()) / 2), -1]:
                a = self.problem.interpolate(
                    self.problem.times()[i],
                    self.problem.times(),
                    self.results[v],
                    0.0,
                    0.0,
                )
                b = self.results[v][i]
                self.assertAlmostEqual(a, b, self.tolerance)


class TestCSVLookupMixin(TestCSVMixin):

    def setUp(self):
        self.problem = ModelLookup()
        self.problem.optimize()
        self.results = self.problem.extract_results()
        self.tolerance = 1e-6

    def test_call(self):
        self.assertAlmostEqual(
            self.problem.lookup_tables(0)["constant_input"](0.2), 2.0, self.tolerance
        )
        self.assertAlmostEqual(
            self.problem.lookup_tables(0)["constant_input"](np.array([0.2, 0.3])),
            np.array([2.0, 3.0]),
            self.tolerance,
        )


class TestPIMixinEnsemble(TestCase):

    def setUp(self):
        self.problem = ModelEnsemble()
        self.problem.optimize()
        self.tolerance = 1e-6

    def test_objective_value(self):
        objective_value_tol = 1e-6
        self.assertTrue(abs(self.problem.objective_value) < objective_value_tol)
