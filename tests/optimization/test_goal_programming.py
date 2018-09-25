import logging

import numpy as np

from rtctools.optimization.collocated_integrated_optimization_problem import (
    CollocatedIntegratedOptimizationProblem
)
from rtctools.optimization.goal_programming_mixin import (
    Goal,
    GoalProgrammingMixin,
    StateGoal,
)
from rtctools.optimization.modelica_mixin import ModelicaMixin
from rtctools.optimization.timeseries import Timeseries

from test_case import TestCase

from .data_path import data_path

logger = logging.getLogger("rtctools")
logger.setLevel(logging.WARNING)


class Model(
    GoalProgrammingMixin, ModelicaMixin, CollocatedIntegratedOptimizationProblem
):

    def __init__(self):
        super().__init__(
            input_folder=data_path(),
            output_folder=data_path(),
            model_name="ModelWithInitial",
            model_folder=data_path(),
        )

    def times(self, variable=None):
        # Collocation points
        return np.linspace(0.0, 1.0, 21)

    def parameters(self, ensemble_member):
        parameters = super().parameters(ensemble_member)
        parameters["u_max"] = 2.0
        return parameters

    def constant_inputs(self, ensemble_member):
        constant_inputs = super().constant_inputs(ensemble_member)
        constant_inputs["constant_input"] = Timeseries(
            np.hstack(([self.initial_time, self.times()])),
            np.hstack(([1.0], np.linspace(1.0, 0.0, 21))),
        )
        return constant_inputs

    def bounds(self):
        bounds = super().bounds()
        bounds["u"] = (-2.0, 2.0)
        return bounds

    def goals(self):
        return [TestGoal1(), TestGoal2(), TestGoal3()]

    def set_timeseries(self, timeseries_id, timeseries, ensemble_member, **kwargs):
        # Do nothing
        pass

    def compiler_options(self):
        compiler_options = super().compiler_options()
        compiler_options["cache"] = False
        return compiler_options


class TestGoal1(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state_at("x", 0.5, ensemble_member=ensemble_member)

    function_range = (-1e1, 1e1)
    priority = 2
    target_min = 0.0
    violation_timeseries_id = "violation"
    function_value_timeseries_id = "function_value"


class TestGoal2(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state_at("x", 0.7, ensemble_member=ensemble_member)

    function_range = (-1e1, 1e1)
    priority = 2
    target_min = 0.1


class TestGoal3(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.integral(
            "x", 0.1, 1.0, ensemble_member=ensemble_member
        )

    function_range = (-1e1, 1e1)
    priority = 1
    target_max = 1.0


class TestGoalProgramming(TestCase):

    def setUp(self):
        self.problem = Model()
        self.problem.optimize()
        self.tolerance = 1e-6

    def test_x(self):
        objective_value_tol = 1e-6
        self.assertAlmostGreaterThan(
            self.problem.interpolate(
                0.7, self.problem.times(), self.problem.extract_results()["x"]
            ),
            0.1,
            objective_value_tol,
        )


class TestGoalNoMinMax(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.integral("x", ensemble_member=ensemble_member)

    function_nominal = 2e1
    priority = 1
    order = 1


class TestGoalLowMax(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.integral("x", ensemble_member=ensemble_member)

    function_range = (-1e1, 1e1)
    priority = 1
    order = 1
    # TODO: Why this number? Is it a coincidence?
    target_max = function_range[0]


# Inherit from existing Model, as all properties are equal except the
# goals.
class ModelNoMinMax(Model):

    def goals(self):
        return [TestGoalNoMinMax()]


class ModelLowMax(Model):

    def goals(self):
        return [TestGoalLowMax()]


class TestGoalProgrammingNoMinMax(TestCase):

    def setUp(self):
        self.problem1 = ModelNoMinMax()
        self.problem2 = ModelLowMax()
        self.problem1.optimize()
        self.problem2.optimize()
        self.tolerance = 1e-6

    def test_nobounds_equal_lowmax(self):
        self.assertAlmostEqual(
            sum(self.problem1.extract_results()["x"]),
            sum(self.problem2.extract_results()["x"]),
            self.tolerance,
        )


class TestGoalMinimizeU(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state_at("u", 0.5, ensemble_member=ensemble_member)

    priority = 1
    order = 1


class TestGoalMinimizeX(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state_at("x", 0.5, ensemble_member=ensemble_member)

    function_range = (-1e2, 1e2)
    priority = 2
    order = 1
    target_min = 2.0


class ModelMinimizeU(Model):

    def goals(self):
        return [TestGoalMinimizeU()]


class ModelMinimizeUandX(Model):

    def goals(self):
        return [TestGoalMinimizeU(), TestGoalMinimizeX()]


class TestGoalProgrammingHoldMinimization(TestCase):

    def setUp(self):
        self.problem1 = ModelMinimizeU()
        self.problem2 = ModelMinimizeUandX()
        self.problem1.optimize()
        self.problem2.optimize()
        self.tolerance = 1e-6

    def test_hold_minimization_goal(self):
        # Collocation point 0.5 is at index 10
        self.assertAlmostEqual(
            self.problem1.extract_results()["u"][10],
            self.problem2.extract_results()["u"][10],
            self.tolerance,
        )


class PathGoal1(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("x")

    function_range = (-1e1, 1e1)
    priority = 1
    target_min = 0.0


class PathGoal2(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("x")

    function_range = (-1e1, 1e1)
    priority = 2
    target_max = Timeseries(np.linspace(0.0, 1.0, 21), 21 * [1.0])


class PathGoal3(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("u")

    priority = 3


class PathGoal4(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("constant_input")

    priority = 4


class PathGoal5(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("k")

    priority = 5


class ModelPathGoals(
    GoalProgrammingMixin, ModelicaMixin, CollocatedIntegratedOptimizationProblem
):

    def __init__(self):
        super().__init__(
            input_folder=data_path(),
            output_folder=data_path(),
            model_name="ModelWithInitial",
            model_folder=data_path(),
        )

    def times(self, variable=None):
        # Collocation points
        return np.linspace(0.0, 1.0, 21)

    def parameters(self, ensemble_member):
        parameters = super().parameters(ensemble_member)
        parameters["u_max"] = 2.0
        return parameters

    def constant_inputs(self, ensemble_member):
        constant_inputs = super().constant_inputs(ensemble_member)
        constant_inputs["constant_input"] = Timeseries(
            np.hstack(([self.initial_time, self.times()])),
            np.hstack(([1.0], np.linspace(1.0, 0.0, 21))),
        )
        return constant_inputs

    def bounds(self):
        bounds = super().bounds()
        bounds["u"] = (-2.0, 2.0)
        return bounds

    def path_goals(self):
        return [PathGoal1(), PathGoal2(), PathGoal3(), PathGoal4(), PathGoal5()]

    def compiler_options(self):
        compiler_options = super().compiler_options()
        compiler_options["cache"] = False
        return compiler_options


class TestGoalProgrammingPathGoals(TestCase):

    def setUp(self):
        self.problem = ModelPathGoals()
        self.problem.optimize()
        self.tolerance = 1e-6

    def test_x(self):
        value_tol = 1e-3
        for x in self.problem.extract_results()["x"]:
            self.assertAlmostGreaterThan(x, 0.0, value_tol)
            self.assertAlmostLessThan(x, 1.1, value_tol)


class PathGoal1Reversed(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("x")

    function_range = (-1e1, 1e1)
    priority = 2
    target_min = 0.0


class PathGoal2Reversed(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("x")

    function_range = (-1e1, 1e1)
    priority = 1
    target_max = Timeseries(np.linspace(0.0, 1.0, 21), 21 * [1.0])


class ModelPathGoalsReversed(ModelPathGoals):

    def path_goals(self):
        return [PathGoal1Reversed(), PathGoal2Reversed()]


class TestGoalProgrammingPathGoalsReversed(TestGoalProgrammingPathGoals):

    def setUp(self):
        self.problem = ModelPathGoalsReversed()
        self.problem.optimize()
        self.tolerance = 1e-6


class TestGoalMinU(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.integral("u", ensemble_member=ensemble_member)

    priority = 3


class ModelPathGoalsMixed(ModelPathGoals):

    def path_goals(self):
        return [PathGoal1(), PathGoal2()]

    def goals(self):
        return [TestGoalMinU()]


class PathGoal1Critical(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("x")

    priority = 1
    target_min = 0.0
    critical = True


class TestGoalLowerUCritical(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.integral("u", ensemble_member=ensemble_member)

    priority = 3
    target_min = 1e-6
    critical = True


class ModelPathGoalsMixedCritical(ModelPathGoals):

    def path_goals(self):
        return [PathGoal1Critical(), PathGoal2()]

    def goals(self):
        return [TestGoalLowerUCritical()]


class TestGoalProgrammingPathGoalsMixed(TestGoalProgrammingPathGoals):

    def setUp(self):
        self.problem = ModelPathGoalsMixed()
        self.problem.optimize()
        self.tolerance = 1e-6


class ModelEnsemble(Model):

    @property
    def ensemble_size(self):
        return 2

    def constant_inputs(self, ensemble_member):
        constant_inputs = super().constant_inputs(ensemble_member)
        constant_inputs["constant_input"] = Timeseries(
            np.hstack(([self.initial_time, self.times()])),
            np.hstack(([1.0], np.linspace(1.0, 0.0, 21))),
        )
        if ensemble_member == 0:
            constant_inputs["constant_input"] = Timeseries(
                self.times(), np.linspace(1.0, 0.0, 21)
            )
        else:
            constant_inputs["constant_input"] = Timeseries(
                self.times(), np.linspace(1.0, 0.5, 21)
            )
        return constant_inputs


class TestGoalProgrammingEnsemble(TestGoalProgramming):

    def setUp(self):
        self.problem = ModelEnsemble()
        self.problem.optimize()
        self.tolerance = 1e-6

    def test_x(self):
        objective_value_tol = 1e-6
        self.assertAlmostGreaterThan(
            self.problem.interpolate(
                0.7, self.problem.times(), self.problem.extract_results(0)["x"]
            ),
            0.1,
            objective_value_tol,
        )
        self.assertAlmostGreaterThan(
            self.problem.interpolate(
                0.7, self.problem.times(), self.problem.extract_results(1)["x"]
            ),
            0.1,
            objective_value_tol,
        )


class PathGoalSmoothing(Goal):

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.der("u")

    priority = 3


class ModelPathGoalsSmoothing(
    GoalProgrammingMixin, ModelicaMixin, CollocatedIntegratedOptimizationProblem
):

    def __init__(self):
        super().__init__(
            input_folder=data_path(),
            output_folder=data_path(),
            model_name="ModelWithInitial",
            model_folder=data_path(),
        )

    def times(self, variable=None):
        # Collocation points
        return np.linspace(0.0, 1.0, 21)

    def parameters(self, ensemble_member):
        parameters = super().parameters(ensemble_member)
        parameters["u_max"] = 2.0
        return parameters

    def constant_inputs(self, ensemble_member):
        constant_inputs = super().constant_inputs(ensemble_member)
        constant_inputs["constant_input"] = Timeseries(
            np.hstack(([self.initial_time, self.times()])),
            np.hstack(([1.0], np.linspace(1.0, 0.0, 21))),
        )
        return constant_inputs

    def bounds(self):
        bounds = super().bounds()
        bounds["u"] = (-2.0, 2.0)
        return bounds

    def path_goals(self):
        return [PathGoal1(), PathGoal2(), PathGoalSmoothing()]

    def compiler_options(self):
        compiler_options = super().compiler_options()
        compiler_options["cache"] = False
        return compiler_options


class TestGoalProgrammingSmoothing(TestCase):

    def setUp(self):
        self.problem = ModelPathGoalsSmoothing()
        self.problem.optimize()
        self.tolerance = 1e-6

    def test_x(self):
        value_tol = 1e-3
        for x in self.problem.extract_results()["x"]:
            self.assertAlmostGreaterThan(x, 0.0, value_tol)
            self.assertAlmostLessThan(x, 1.1, value_tol)


class StateGoal1(StateGoal):

    state = "x"
    priority = 1
    target_min = 0.0
    violation_timeseries_id = "violation2"
    function_value_timeseries_id = "function_value2"


class StateGoal2(StateGoal):

    state = "x"
    priority = 2
    target_max = Timeseries(np.linspace(0.0, 1.0, 21), 21 * [1.0])


class StateGoal3(StateGoal):

    state = "u"
    priority = 3


class ModelStateGoals(
    GoalProgrammingMixin, ModelicaMixin, CollocatedIntegratedOptimizationProblem
):

    def __init__(self):
        super().__init__(
            input_folder=data_path(),
            output_folder=data_path(),
            model_name="ModelWithInitial",
            model_folder=data_path(),
        )

    def times(self, variable=None):
        # Collocation points
        return np.linspace(0.0, 1.0, 21)

    def parameters(self, ensemble_member):
        parameters = super().parameters(ensemble_member)
        parameters["u_max"] = 2.0
        return parameters

    def constant_inputs(self, ensemble_member):
        constant_inputs = super().constant_inputs(ensemble_member)
        constant_inputs["constant_input"] = Timeseries(
            np.hstack(([self.initial_time, self.times()])),
            np.hstack(([1.0], np.linspace(1.0, 0.0, 21))),
        )
        return constant_inputs

    def bounds(self):
        bounds = super().bounds()
        bounds["u"] = (-2.0, 2.0)
        bounds["x"] = (-10, 10)
        return bounds

    def path_goals(self):
        return [StateGoal1(self), StateGoal2(self), StateGoal3(self)]

    def set_timeseries(self, timeseries_id, timeseries, ensemble_member, **kwargs):
        # Do nothing
        pass

    def compiler_options(self):
        compiler_options = super().compiler_options()
        compiler_options["cache"] = False
        return compiler_options


class TestGoalProgrammingStateGoals(TestCase):

    def setUp(self):
        self.problem = ModelStateGoals()
        self.problem.optimize()
        self.tolerance = 1e-6

    def test_x(self):
        value_tol = 1e-3
        for x in self.problem.extract_results()["x"]:
            self.assertAlmostGreaterThan(x, 0.0, value_tol)
            self.assertAlmostLessThan(x, 1.1, value_tol)


class ModelMinimizeTwoGoals(ModelMinimizeUandX):
    def __init__(self, *args, scale_by_problem_size=False, **kwargs):
        super().__init__(*args, **kwargs)

        self.scale_by_problem_size = scale_by_problem_size

    def goal_programming_options(self):
        options = super().goal_programming_options()

        if self.scale_by_problem_size:
            options['scale_by_problem_size'] = True

        return options

    def goals(self):
        goals = super().goals()
        for g in goals:
            g.priority = 1
        return goals


class TestPathGoalMinimizeU(Goal):
    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("u")

    priority = 1
    order = 1


class TestPathGoalMinimizeX(Goal):
    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state("x")

    priority = 1
    order = 1


class ModelMinimizeTwoPathGoals(Model):
    def __init__(self, *args, scale_by_problem_size=False, **kwargs):
        super().__init__(*args, **kwargs)

        self.scale_by_problem_size = scale_by_problem_size

    def goal_programming_options(self):
        options = super().goal_programming_options()

        if self.scale_by_problem_size:
            options['scale_by_problem_size'] = True

        return options

    def goals(self):
        return []

    def path_goals(self):
        return [TestPathGoalMinimizeU(), TestPathGoalMinimizeX()]


class ModelMinimizeTwoTargetPathGoals(ModelMinimizeTwoPathGoals):
    def path_goals(self):
        goals = super().path_goals()
        for g in goals:
            g.function_range = (-2.0, 10.0)
            g.target_min = 2.0
            # To make sure the objective contains enough significant digits to
            # compare, we make it a bit larger with the weight
            g.weight = 100
        return goals


class TestScaleByProblemSize(TestCase):
    tolerance = 1e-5

    def test_goals_scale_by_problem_size(self):
        self.problem1 = ModelMinimizeTwoGoals()
        self.problem2 = ModelMinimizeTwoGoals(scale_by_problem_size=True)
        self.problem1.optimize()
        self.problem2.optimize()

        obj_value_no_scale = self.problem1.objective_value
        obj_value_scale = self.problem2.objective_value

        self.assertAlmostEqual(1.0, 2 * obj_value_scale / obj_value_no_scale, self.tolerance)

    def test_path_minimization_goals_scale_by_problem_size(self):
        self.problem1 = ModelMinimizeTwoPathGoals()
        self.problem2 = ModelMinimizeTwoPathGoals(scale_by_problem_size=True)
        self.problem1.optimize()
        self.problem2.optimize()

        n_times = len(self.problem2.times())

        obj_value_no_scale = self.problem1.objective_value
        obj_value_scale = self.problem2.objective_value

        self.assertAlmostEqual(1.0, 2 * n_times * obj_value_scale / obj_value_no_scale, self.tolerance)

    def test_path_target_goals_scale_by_problem_size(self):
        self.problem1 = ModelMinimizeTwoTargetPathGoals()
        self.problem2 = ModelMinimizeTwoTargetPathGoals(scale_by_problem_size=True)
        self.problem1.optimize()
        self.problem2.optimize()

        n_times = len(self.problem2.times())

        obj_value_no_scale = self.problem1.objective_value
        obj_value_scale = self.problem2.objective_value

        self.assertAlmostEqual(1.0, 2 * n_times * obj_value_scale / obj_value_no_scale, self.tolerance)


class ModelInvalidGoals(Model):
    _goals = []

    def goals(self):
        return self._goals


class InvalidGoal(Goal):

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def function(self, optimization_problem, ensemble_member):
        return optimization_problem.state_at("x", 0.5, ensemble_member=ensemble_member)


class TestGoalProgrammingInvalidGoals(TestCase):

    def setUp(self):
        self.problem = ModelInvalidGoals()

    def test_target_min_lt_function_range_lb(self):
        self.problem._goals = [InvalidGoal(function_range=(-2.0, 2.0), target_min=-3.0)]
        with self.assertRaisesRegexp(Exception, "minimum should be greater than the lower"):
            self.problem.optimize()

    def test_target_min_eq_function_range_lb(self):
        self.problem._goals = [InvalidGoal(function_range=(-2.0, 2.0), target_min=-2.0)]
        with self.assertRaisesRegexp(Exception, "minimum should be greater than the lower"):
            self.problem.optimize()

    def test_target_max_gt_function_range_ub(self):
        self.problem._goals = [InvalidGoal(function_range=(-2.0, 2.0), target_max=3.0)]
        with self.assertRaisesRegexp(Exception, "maximum should be smaller than the upper"):
            self.problem.optimize()

    def test_target_max_eq_function_range_ub(self):
        self.problem._goals = [InvalidGoal(function_range=(-2.0, 2.0), target_max=2.0)]
        with self.assertRaisesRegexp(Exception, "maximum should be smaller than the upper"):
            self.problem.optimize()

    def test_critical_minimization(self):
        self.problem._goals = [InvalidGoal(critical=True)]
        with self.assertRaisesRegexp(Exception, "Minimization goals cannot be critical"):
            self.problem.optimize()

    def test_minimization_function_range(self):
        self.problem._goals = [InvalidGoal(function_range=(-2.0, 2.0))]
        with self.assertRaisesRegexp(Exception, "Specifying function range not allowed"):
            self.problem.optimize()

    def test_function_range_present(self):
        self.problem._goals = [InvalidGoal(target_min=2.0)]
        with self.assertRaisesRegexp(Exception, "No function range specified"):
            self.problem.optimize()

    def test_function_range_valid(self):
        self.problem._goals = [InvalidGoal(function_range=(2.0, -2.0), target_min=2.1)]
        with self.assertRaisesRegexp(Exception, "Invalid function range"):
            self.problem.optimize()

    def test_function_nominal_positive(self):
        self.problem._goals = [InvalidGoal(function_nominal=-1.0)]
        with self.assertRaisesRegexp(Exception, "Nonpositive nominal value"):
            self.problem.optimize()

        self.problem._goals = [InvalidGoal(function_nominal=0.0)]
        with self.assertRaisesRegexp(Exception, "Nonpositive nominal value"):
            self.problem.optimize()

    def test_priority_not_cast_int(self):
        self.problem._goals = [InvalidGoal(priority='test')]
        with self.assertRaisesRegexp(Exception, "castable to int"):
            self.problem.optimize()
