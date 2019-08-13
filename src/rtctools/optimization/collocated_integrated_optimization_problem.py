import itertools
import logging
from abc import ABCMeta, abstractmethod

import casadi as ca

import numpy as np

from rtctools._internal.alias_tools import AliasDict
from rtctools._internal.casadi_helpers import \
    interpolate, is_affine, nullvertcat, reduce_matvec, substitute_in_external

from .optimization_problem import OptimizationProblem
from .timeseries import Timeseries

logger = logging.getLogger("rtctools")


class CollocatedIntegratedOptimizationProblem(OptimizationProblem, metaclass=ABCMeta):
    """
    Discretizes your model using a mixed collocation/integration scheme.

    Collocation means that the discretized model equations are included as constraints
    between state variables in the optimization problem.

    .. note::

        To ensure that your optimization problem only has globally optimal solutions,
        any model equations that are collocated must be linear.  By default, all
        model equations are collocated, and linearity of the model equations is
        verified.  Working with non-linear models is possible, but discouraged.

    :cvar check_collocation_linearity:
        If ``True``, check whether collocation constraints are linear. Default is ``True``.
    """

    #: Check whether the collocation constraints are linear
    check_collocation_linearity = True

    #: Whether or not the collocation constraints are linear (affine)
    linear_collocation = None

    def __init__(self, **kwargs):
        # Variables that will be optimized
        self.dae_variables['free_variables'] = self.dae_variables[
            'states'] + self.dae_variables['algebraics'] + self.dae_variables['control_inputs']

        # Cache names of states
        self.__differentiated_states = [variable.name() for variable in self.dae_variables['states']]
        self.__differentiated_states_map = {v: i for i, v in enumerate(self.__differentiated_states)}

        self.__algebraic_states = [variable.name()
                                   for variable in self.dae_variables['algebraics']]
        self.__algebraic_states_map = {v: i for i, v in enumerate(self.__algebraic_states)}

        self.__controls = [variable.name()
                           for variable in self.dae_variables['control_inputs']]
        self.__controls_map = {v: i for i, v in enumerate(self.__controls)}

        self.__derivative_names = [variable.name() for variable in self.dae_variables['derivatives']]

        # DAE cache
        self.__integrator_step_function = None
        self.__dae_residual_function_collocated = None
        self.__initial_residual_with_params_fun_map = None

        # Create dictionary of variables so that we have O(1) state lookup available
        self.__variables = AliasDict(self.alias_relation)
        for var in itertools.chain(
                self.dae_variables['states'],
                self.dae_variables['algebraics'],
                self.dae_variables['control_inputs'],
                self.dae_variables['constant_inputs'],
                self.dae_variables['parameters'],
                self.dae_variables['time']):
            self.__variables[var.name()] = var

        self.__orig_variables = AliasDict(self.alias_relation)
        self.__orig_variables.update(self.__variables)
        # self.__orig_variables = self.__variables.copy()

        # Call super
        super().__init__(**kwargs)

    @abstractmethod
    def times(self, variable=None):
        """
        List of time stamps for variable.

        :param variable: Variable name.

        :returns: A list of time stamps for the given variable.
        """
        pass

    def interpolation_method(self, variable=None):
        """
        Interpolation method for variable.

        :param variable: Variable name.

        :returns: Interpolation method for the given variable.
        """
        return self.INTERPOLATION_LINEAR

    @property
    def integrated_states(self):
        """
        A list of states that are integrated rather than collocated.

        .. warning:: This is an experimental feature.
        """
        return []

    @property
    def theta(self):
        r"""
        RTC-Tools discretizes differential equations of the form

        .. math::

            \dot{x} = f(x, u)

        using the :math:`\theta`-method

        .. math::

            x_{i+1} = x_i + \Delta t \left[\theta f(x_{i+1}, u_{i+1}) + (1 - \theta) f(x_i, u_i)\right]

        The default is :math:`\theta = 1`, resulting in the implicit or backward Euler method.  Note that in this
        case, the control input at the initial time step is not used.

        Set :math:`\theta = 0` to use the explicit or forward Euler method.  Note that in this
        case, the control input at the final time step is not used.

        .. warning:: This is an experimental feature for :math:`0 < \theta < 1`.
        """

        # Default to implicit Euler collocation, which is cheaper to evaluate
        # than the trapezoidal method, while being A-stable.
        #
        # N.B.  Setting theta to 0 will cause problems with algebraic equations,
        #       unless a consistent initialization is supplied for the algebraics.
        # N.B.  Setting theta to any value strictly between 0 and 1 will cause
        #       algebraic equations to be solved in an average sense.  This may
        #       induce unexpected oscillations.
        # TODO Fix these issue by performing index reduction and splitting DAE into ODE and algebraic parts.
        #      Theta then only applies to the ODE part.
        return 1.0

    def transcribe(self):
        # DAE residual
        dae_residual = self.dae_residual

        # Initial residual
        initial_residual = self.initial_residual

        logger.info(
            'Transcribing problem with a DAE of {} equations, {} collocation points, and {} free variables'.format(
                dae_residual.size1(), len(self.times()), len(self.dae_variables['free_variables'])))

        # Reset dictionary of variables
        self.__variables = AliasDict(self.alias_relation)
        self.__variables.update(self.__orig_variables)
        for var in itertools.chain(self.path_variables, self.extra_variables):
            self.__variables[var.name()] = var

        # Split the constant inputs into those used in the DAE, and additional
        # ones used for just the objective and/or constraints
        dae_constant_inputs_names = [x.name() for x in self.dae_variables['constant_inputs']]
        extra_constant_inputs_name_and_size = []
        for ensemble_member in range(self.ensemble_size):
            extra_constant_inputs_name_and_size.extend(
                [(x, v.values.shape[1] if v.values.ndim > 1 else 1)
                 for x, v in self.constant_inputs(ensemble_member).items()
                 if x not in dae_constant_inputs_names])

        self.__extra_constant_inputs = []
        for var_name, size in extra_constant_inputs_name_and_size:
            var = ca.MX.sym(var_name, size)
            self.__variables[var_name] = var
            self.__extra_constant_inputs.append(var)

        # Cache extra and path variable names, and variable sizes
        self.__path_variable_names = [variable.name()
                                      for variable in self.path_variables]
        self.__extra_variable_names = [variable.name()
                                       for variable in self.extra_variables]

        # Cache the variable sizes, as a repeated call to .name() and .size1()
        # is expensive due to SWIG call overhead.
        self.__variable_sizes = {}

        for variable in itertools.chain(self.differentiated_states, self.algebraic_states):
            self.__variable_sizes[variable] = 1

        for mx_symbol, variable in zip(self.path_variables, self.__path_variable_names):
            self.__variable_sizes[variable] = mx_symbol.size1()

        for mx_symbol, variable in zip(self.extra_variables, self.__extra_variable_names):
            self.__variable_sizes[variable] = mx_symbol.size1()

        # Cache the initial step sizes. We assume that the history has
        # (roughly) identical time steps for the entire ensemble.
        self.__initial_dt = {}
        history_0 = self.history(0)
        for variable in self.differentiated_states:
            times = self.times(variable)
            try:
                h = history_0[variable]
                if h.times[0] == times[0] or len(h.values) == 1:
                    dt = times[1] - times[0]
                else:
                    assert h.times[-1] == times[0]
                    dt = h.times[-1] - h.times[-2]
            except KeyError:
                dt = times[1] - times[0]
            self.__initial_dt[variable] = dt

        # Variables that are integrated states are not yet allowed to have size > 1
        for variable in self.integrated_states:
            if self.__variable_sizes.get(variable, 1) > 1:
                raise NotImplementedError("Vector symbol not supported for integrated state '{}'".format(variable))

        # The same holds for controls
        for variable in self.controls:
            if self.__variable_sizes.get(variable, 1) > 1:
                raise NotImplementedError("Vector symbol not supported for control state '{}'".format(variable))

        # Collocation times
        collocation_times = self.times()
        n_collocation_times = len(collocation_times)

        # Dynamic parameters
        dynamic_parameters = self.dynamic_parameters()
        dynamic_parameter_names = set()

        # Parameter symbols
        symbolic_parameters = ca.vertcat(*self.dae_variables['parameters'])

        # Create a store of all ensemble-member-specific data for all ensemble members
        # N.B. Don't use n * [{}], as it creates n refs to the same dict.
        ensemble_store = [{} for i in range(self.ensemble_size)]
        for ensemble_member in range(self.ensemble_size):
            ensemble_data = ensemble_store[ensemble_member]

            # Store parameters
            parameters = self.parameters(ensemble_member)
            parameter_values = [None] * len(self.dae_variables['parameters'])
            for i, symbol in enumerate(self.dae_variables['parameters']):
                variable = symbol.name()
                try:
                    parameter_values[i] = parameters[variable]
                except KeyError:
                    raise Exception(
                        "No value specified for parameter {}".format(variable))

            if len(dynamic_parameters) > 0:
                jac_1 = ca.jacobian(symbolic_parameters, ca.vertcat(*dynamic_parameters))
                jac_2 = ca.jacobian(ca.vertcat(*parameter_values), ca.vertcat(*dynamic_parameters))
                for i, symbol in enumerate(self.dae_variables['parameters']):
                    if jac_1[i, :].nnz() > 0 or jac_2[i, :].nnz() > 0:
                        dynamic_parameter_names.add(symbol.name())

            if np.any([isinstance(value, ca.MX) and not value.is_constant() for value in parameter_values]):
                parameter_values = nullvertcat(*parameter_values)
                [parameter_values] = substitute_in_external(
                    [parameter_values], self.dae_variables['parameters'], parameter_values)
            else:
                parameter_values = nullvertcat(*parameter_values)

            if ensemble_member == 0:
                # Store parameter values of member 0, as variable bounds may depend on these.
                self.__parameter_values_ensemble_member_0 = parameter_values
            ensemble_data["parameters"] = parameter_values

            # Store constant inputs
            raw_constant_inputs = self.constant_inputs(ensemble_member)

            def _interpolate_constant_inputs(variables):
                constant_inputs_interpolated = {}
                for variable in variables:
                    variable = variable.name()
                    try:
                        constant_input = raw_constant_inputs[variable]
                    except KeyError:
                        raise Exception(
                            "No values found for constant input {}".format(variable))
                    else:
                        values = constant_input.values
                        interpolation_method = self.interpolation_method(variable)
                        constant_inputs_interpolated[variable] = self.interpolate(
                            collocation_times, constant_input.times, values, 0.0, 0.0, interpolation_method)

                return constant_inputs_interpolated

            ensemble_data["constant_inputs"] = _interpolate_constant_inputs(
                self.dae_variables['constant_inputs'])
            ensemble_data["extra_constant_inputs"] = _interpolate_constant_inputs(
                self.__extra_constant_inputs)

            # Handle all extra constant input data uniformly as 2D arrays
            for k, v in ensemble_data["extra_constant_inputs"].items():
                if v.ndim == 1:
                    ensemble_data["extra_constant_inputs"][k] = v[:, None]

        bounds = self.bounds()

        # Initialize control discretization
        control_size, discrete_control, lbx_control, ubx_control, x0_control, indices_control = \
            self.discretize_controls(bounds)

        # Initialize state discretization
        state_size, discrete_state, lbx_state, ubx_state, x0_state, indices_state = \
            self.discretize_states(bounds)

        # Merge state vector offset dictionary
        self.__indices = indices_control
        for ensemble_member in range(self.ensemble_size):
            for key, value in indices_state[ensemble_member].items():
                if isinstance(value, slice):
                    value = slice(value.start + control_size, value.stop + control_size)
                else:
                    value += control_size
                self.__indices[ensemble_member][key] = value

        # Initialize vector of optimization symbols
        X = ca.MX.sym('X', control_size + state_size)
        self.__solver_input = X

        # Later on, we will be slicing MX/SX objects a few times for vectorized operations (to
        # reduce the overhead induced for each CasADi call). When slicing MX/SX objects, we want
        # to do that with a list of Python ints. Slicing with something else (e.g. a list of
        # np.int32, or a numpy array) is significantly slower.
        x_inds = list(range(X.size1()))
        self.__indices_as_lists = [{} for ensemble_member in range(self.ensemble_size)]

        for ensemble_member in range(self.ensemble_size):
            for k, v in self.__indices[ensemble_member].items():
                if isinstance(v, slice):
                    self.__indices_as_lists[ensemble_member][k] = x_inds[v]
                elif isinstance(v, int):
                    self.__indices_as_lists[ensemble_member][k] = [v]
                else:
                    self.__indices_as_lists[ensemble_member][k] = [int(i) for i in v]

        # Initialize bound and seed vectors
        discrete = np.zeros(X.size1(), dtype=np.bool)

        lbx = -np.inf * np.ones(X.size1())
        ubx = np.inf * np.ones(X.size1())

        x0 = np.zeros(X.size1())

        discrete[:len(discrete_control)] = discrete_control
        discrete[len(discrete_control):] = discrete_state
        lbx[:len(lbx_control)] = lbx_control
        lbx[len(lbx_control):] = lbx_state
        ubx[:len(ubx_control)] = ubx_control
        ubx[len(lbx_control):] = ubx_state
        x0[:len(x0_control)] = x0_control
        x0[len(x0_control):] = x0_state

        # Provide a state for self.state_at() and self.der() to work with.
        self.__control_size = control_size
        self.__state_size = state_size
        self.__symbol_cache = {}

        # Free variables for the collocated optimization problem
        integrated_variables = []
        collocated_variables = []
        for variable in itertools.chain(self.dae_variables['states'], self.dae_variables['algebraics']):
            if variable.name() in self.integrated_states:
                integrated_variables.append(variable)
            else:
                collocated_variables.append(variable)
        for variable in self.dae_variables['control_inputs']:
            # TODO treat these separately.
            collocated_variables.append(variable)

        if logger.getEffectiveLevel() == logging.DEBUG:
            logger.debug("Integrating variables {}".format(
                repr(integrated_variables)))
            logger.debug("Collocating variables {}".format(
                repr(collocated_variables)))

        integrated_variable_names = [v.name() for v in integrated_variables]
        integrated_variable_nominals = np.array([self.variable_nominal(v) for v in integrated_variable_names])

        collocated_variable_names = [v.name() for v in collocated_variables]
        collocated_variable_nominals = np.array([self.variable_nominal(v) for v in collocated_variable_names])

        # Split derivatives into "integrated" and "collocated" lists.
        integrated_derivatives = []
        collocated_derivatives = []
        for k, var in enumerate(self.dae_variables['states']):
            if var.name() in self.integrated_states:
                integrated_derivatives.append(
                    self.dae_variables['derivatives'][k])
            else:
                collocated_derivatives.append(
                    self.dae_variables['derivatives'][k])
        self.__algebraic_and_control_derivatives = []
        for var in itertools.chain(self.dae_variables['algebraics'], self.dae_variables['control_inputs']):
            sym = ca.MX.sym('der({})'.format(var.name()))
            self.__algebraic_and_control_derivatives.append(sym)
            collocated_derivatives.append(sym)

        # Path objective
        path_objective = self.path_objective(0)

        # Path constraints
        path_constraints = self.path_constraints(0)
        path_constraint_expressions = ca.vertcat(
            *[f_constraint for (f_constraint, lb, ub) in path_constraints])

        # Delayed feedback
        delayed_feedback_expressions, delayed_feedback_states, delayed_feedback_durations = [], [], []
        delayed_feedback = self.delayed_feedback()
        if delayed_feedback:
            delayed_feedback_expressions, delayed_feedback_states, delayed_feedback_durations = \
                zip(*delayed_feedback)
        # Make sure the original data cannot be used anymore, because it will
        # become incorrect/stale with the inlining of constant parameters.
        del delayed_feedback

        # Initial time
        t0 = self.initial_time

        # Establish integrator theta
        theta = self.theta

        # Set CasADi function options
        options = self.solver_options()
        function_options = {'max_num_dir': options['optimized_num_dir']}

        # Update the store of all ensemble-member-specific data for all ensemble members
        # with initial states, derivatives, and path variables.
        # Use vectorized approach to avoid SWIG call overhead for each CasADi call.
        ensemble_member_size = int(self.__state_size / self.ensemble_size)

        n = len(integrated_variables) + len(collocated_variables)
        for ensemble_member in range(self.ensemble_size):
            ensemble_data = ensemble_store[ensemble_member]

            initial_state_indices = [None] * n

            # Derivatives take a bit more effort to vectorize, as we can have
            # both constant values and elements in the state vector
            initial_derivatives = ca.MX.zeros((n, 1))
            init_der_variable = []
            init_der_variable_indices = []
            init_der_variable_nominals = []
            init_der_constant = []
            init_der_constant_values = []
            init_dt = []

            der_offset = (control_size
                          + (ensemble_member + 1) * ensemble_member_size
                          - len(self.dae_variables['derivatives']))

            history = self.history(ensemble_member)

            for j, variable in enumerate(integrated_variable_names + collocated_variable_names):
                initial_state_indices[j] = self.__indices_as_lists[ensemble_member][variable][0]

                try:
                    i = self.__differentiated_states_map[variable]

                    init_der_variable_nominals.append(self.variable_nominal(variable))
                    init_der_variable_indices.append(der_offset + i)
                    init_der_variable.append(j)
                    init_dt.append(self.__initial_dt[variable])

                except KeyError:
                    # We do interpolation here instead of relying on der_at. This faster is because:
                    # 1. We can reuse the history variable.
                    # 2. We know that "variable" is a canonical state
                    # 3. We know that we are only dealing with history (numeric values, not symbolics)
                    try:
                        h = history[variable]
                        if h.times[0] == t0 or len(h.values) == 1:
                            init_der = 0.0
                        else:
                            assert h.times[-1] == t0
                            init_der = (h.values[-1] - h.values[-2])/(h.times[-1] - h.times[-2])
                    except KeyError:
                        init_der = 0.0

                    init_der_constant_values.append(init_der)
                    init_der_constant.append(j)

            initial_derivatives[init_der_variable] = X[init_der_variable_indices] / np.array(
                init_dt) * np.array(init_der_variable_nominals)
            if len(init_der_constant_values) > 0:
                initial_derivatives[init_der_constant] = init_der_constant_values

            ensemble_data["initial_state"] = X[initial_state_indices] * np.concatenate(
                (integrated_variable_nominals, collocated_variable_nominals))
            ensemble_data["initial_derivatives"] = initial_derivatives

            # Store initial path variables
            initial_path_variable_inds = []

            path_variables_size = sum(self.__variable_sizes[v] for v in self.__path_variable_names)
            path_variables_nominals = np.ones(path_variables_size)

            offset = 0
            for variable in self.__path_variable_names:
                step = len(self.times(variable))
                initial_path_variable_inds.extend(self.__indices_as_lists[ensemble_member][variable][0::step])

                variable_size = self.__variable_sizes[variable]
                path_variables_nominals[offset:offset + variable_size] = self.variable_nominal(variable)
                offset += variable_size

            ensemble_data["initial_path_variables"] = X[initial_path_variable_inds] * path_variables_nominals

        # Replace parameters which are constant across the entire ensemble
        constant_parameters = []
        constant_parameter_values = []

        ensemble_parameters = []
        ensemble_parameter_values = [[] for i in range(self.ensemble_size)]

        for i, parameter in enumerate(self.dae_variables['parameters']):
            values = [ensemble_store[ensemble_member]["parameters"][i] for ensemble_member in range(self.ensemble_size)]
            if ((len(values) == 1 or (np.all(values) == values[0]))
                    and parameter.name() not in dynamic_parameter_names):
                constant_parameters.append(parameter)
                constant_parameter_values.append(values[0])
            else:
                ensemble_parameters.append(parameter)
                for ensemble_member in range(self.ensemble_size):
                    ensemble_parameter_values[ensemble_member].append(values[ensemble_member])

        symbolic_parameters = ca.vertcat(*ensemble_parameters)

        # Inline constant parameter values
        if constant_parameters:
            delayed_feedback_expressions = ca.substitute(
                delayed_feedback_expressions,
                constant_parameters,
                constant_parameter_values)

            delayed_feedback_durations = ca.substitute(
                delayed_feedback_durations,
                constant_parameters,
                constant_parameter_values)

            path_objective, path_constraint_expressions = \
                ca.substitute(
                    [path_objective, path_constraint_expressions],
                    constant_parameters,
                    constant_parameter_values)

        # Collect extra variable symbols
        symbolic_extra_variables = ca.vertcat(*self.extra_variables)

        # Aggregate ensemble data
        ensemble_aggregate = {}
        ensemble_aggregate["parameters"] = ca.horzcat(*[nullvertcat(*l) for l in ensemble_parameter_values])
        ensemble_aggregate["initial_constant_inputs"] = ca.horzcat(*[
            nullvertcat(*[
                float(d["constant_inputs"][variable.name()][0])
                for variable in self.dae_variables['constant_inputs']])
            for d in ensemble_store])
        ensemble_aggregate["initial_extra_constant_inputs"] = ca.horzcat(*[
            nullvertcat(*[
                d["extra_constant_inputs"][variable.name()][0, :]
                for variable in self.__extra_constant_inputs])
            for d in ensemble_store])
        ensemble_aggregate["initial_state"] = ca.horzcat(
            *[d["initial_state"] for d in ensemble_store])
        ensemble_aggregate["initial_state"] = reduce_matvec(
            ensemble_aggregate["initial_state"], self.solver_input)
        ensemble_aggregate["initial_derivatives"] = ca.horzcat(
            *[d["initial_derivatives"] for d in ensemble_store])
        ensemble_aggregate["initial_derivatives"] = reduce_matvec(
            ensemble_aggregate["initial_derivatives"], self.solver_input)
        ensemble_aggregate["initial_path_variables"] = ca.horzcat(
            *[d["initial_path_variables"] for d in ensemble_store])
        ensemble_aggregate["initial_path_variables"] = reduce_matvec(
            ensemble_aggregate["initial_path_variables"], self.solver_input)

        if (self.__dae_residual_function_collocated is None) and (self.__integrator_step_function is None):
            # Insert lookup tables.  No support yet for different lookup tables per ensemble member.
            lookup_tables = self.lookup_tables(0)

            for sym in self.dae_variables['lookup_tables']:
                sym_name = sym.name()

                try:
                    lookup_table = lookup_tables[sym_name]
                except KeyError:
                    raise Exception(
                        "Unable to find lookup table function for {}".format(sym_name))
                else:
                    input_syms = [self.variable(input_sym.name())
                                  for input_sym in lookup_table.inputs]

                    value = lookup_table.function(*input_syms)
                    [dae_residual] = ca.substitute(
                        [dae_residual], [sym], [value])

            if len(self.dae_variables['lookup_tables']) > 0 and self.ensemble_size > 1:
                logger.warning(
                    "Using lookup tables of ensemble member #0 for all members.")

            # Insert constant parameter values
            dae_residual, initial_residual = \
                ca.substitute(
                    [dae_residual, initial_residual],
                    constant_parameters,
                    constant_parameter_values)

            # Split DAE into integrated and into a collocated part
            dae_residual_integrated = []
            dae_residual_collocated = []

            dae_outputs = ca.vertsplit(dae_residual)
            for output in dae_outputs:
                contains = False
                for derivative in integrated_derivatives:
                    if ca.depends_on(output, derivative):
                        contains = True
                        break

                if contains:
                    dae_residual_integrated.append(output)
                else:
                    dae_residual_collocated.append(output)
            dae_residual_integrated = ca.vertcat(*dae_residual_integrated)
            dae_residual_collocated = ca.vertcat(*dae_residual_collocated)

            # Check linearity of collocated part
            if self.check_collocation_linearity and dae_residual_collocated.size1() > 0:
                # Check linearity of collocation constraints, which is a necessary condition for the
                # optimization problem to be convex
                self.linear_collocation = True

                # Aside from decision variables, the DAE expression also contains parameters
                # and constant inputs. We need to inline them before we do the affinity check.
                # Note that this not an exhaustive check, as other values for the
                # parameters/constant inputs may result in a non-affine DAE (or vice-versa).
                np.random.seed(42)
                fixed_vars = ca.vertcat(*self.dae_variables['time'],
                                        *self.dae_variables['constant_inputs'],
                                        ca.MX(symbolic_parameters))
                fixed_var_values = np.random.rand(fixed_vars.size1())

                if not is_affine(ca.substitute(dae_residual_collocated, fixed_vars, fixed_var_values),
                                 ca.vertcat(* collocated_variables + integrated_variables +
                                            collocated_derivatives + integrated_derivatives)):
                    self.linear_collocation = False

                    logger.warning(
                        'The DAE residual contains equations that are not affine. '
                        'There is therefore no guarantee that the optimization problem is convex. '
                        'This will, in general, result in the existence of multiple local optima '
                        'and trouble finding a feasible initial solution.')

            # Transcribe DAE using theta method collocation
            if len(integrated_variables) > 0:
                I = ca.MX.sym('I', len(integrated_variables))  # noqa: E741
                I0 = ca.MX.sym('I0', len(integrated_variables))
                C0 = [ca.MX.sym('C0[{}]'.format(i))
                      for i in range(len(collocated_variables))]
                CI0 = [ca.MX.sym('CI0[{}]'.format(i))
                       for i in range(len(self.dae_variables['constant_inputs']))]
                dt_sym = ca.MX.sym('dt')

                integrated_finite_differences = (I - I0) / dt_sym

                [dae_residual_integrated_0] = ca.substitute(
                    [dae_residual_integrated],
                    (integrated_variables +
                     collocated_variables +
                     integrated_derivatives +
                     self.dae_variables['constant_inputs'] +
                     self.dae_variables['time']),
                    ([I0[i] for i in range(len(integrated_variables))] +
                     [C0[i] for i in range(len(collocated_variables))] +
                     [integrated_finite_differences[i] for i in range(len(integrated_derivatives))] +
                     [CI0[i] for i in range(len(self.dae_variables['constant_inputs']))] +
                     [self.dae_variables['time'][0] - dt_sym]))
                [dae_residual_integrated_1] = ca.substitute(
                    [dae_residual_integrated],
                    (integrated_variables +
                     integrated_derivatives),
                    ([I[i] for i in range(len(integrated_variables))] +
                     [integrated_finite_differences[i] for i in range(len(integrated_derivatives))]))

                if theta == 0:
                    dae_residual_integrated = dae_residual_integrated_0
                elif theta == 1:
                    dae_residual_integrated = dae_residual_integrated_1
                else:
                    dae_residual_integrated = (
                        1 - theta) * dae_residual_integrated_0 + theta * dae_residual_integrated_1

                dae_residual_function_integrated = ca.Function(
                    'dae_residual_function_integrated',
                    [I,
                     I0,
                     symbolic_parameters,
                     ca.vertcat(*(
                        [C0[i] for i in range(len(collocated_variables))] +
                        [CI0[i] for i in range(len(self.dae_variables['constant_inputs']))] +
                        [dt_sym] +
                        collocated_variables +
                        collocated_derivatives +
                        self.dae_variables['constant_inputs'] +
                        self.dae_variables['time']))],
                    [dae_residual_integrated],
                    function_options)

                # if not self.dae_is_external_function:
                try:
                    dae_residual_function_integrated = dae_residual_function_integrated.expand()
                except RuntimeError as e:
                    # We only expect to fail if the DAE was an external function
                    if "'eval_sx' not defined for External" in str(e):
                        pass
                    else:
                        raise

                options = self.integrator_options()
                self.__integrator_step_function = ca.rootfinder(
                    'integrator_step_function', 'newton', dae_residual_function_integrated, options)

            # Initialize a Function for the DAE residual (collocated part)
            if len(collocated_variables) > 0:
                self.__dae_residual_function_collocated = ca.Function(
                    'dae_residual_function_collocated',
                    [symbolic_parameters,
                     ca.vertcat(*(
                        integrated_variables +
                        collocated_variables +
                        integrated_derivatives +
                        collocated_derivatives +
                        self.dae_variables['constant_inputs'] +
                        self.dae_variables['time']))],
                    [dae_residual_collocated],
                    function_options)
                try:
                    self.__dae_residual_function_collocated = self.__dae_residual_function_collocated.expand()
                except RuntimeError as e:
                    # We only expect to fail if the DAE was an external function
                    if "'eval_sx' not defined for External" in str(e):
                        pass
                    else:
                        raise

        if len(integrated_variables) > 0:
            integrator_step_function = self.__integrator_step_function
        if len(collocated_variables) > 0:
            dae_residual_function_collocated = self.__dae_residual_function_collocated
            dae_residual_collocated_size = dae_residual_function_collocated.mx_out(
                0).size1()
        else:
            dae_residual_collocated_size = 0

        # Note that this list is stored, such that it can be reused in the
        # map_path_expression() method.
        self.__func_orig_inputs = [
            symbolic_parameters,
            ca.vertcat(
                *integrated_variables, *collocated_variables, *integrated_derivatives,
                *collocated_derivatives, *self.dae_variables['constant_inputs'],
                *self.dae_variables['time'], *self.path_variables,
                *self.__extra_constant_inputs),
            symbolic_extra_variables]

        # Initialize a Function for the path objective
        # Note that we assume that the path objective expression is the same for all ensemble members
        path_objective_function = ca.Function(
            'path_objective',
            self.__func_orig_inputs,
            [path_objective],
            function_options)
        path_objective_function = path_objective_function.expand()

        # Initialize a Function for the path constraints
        # Note that we assume that the path constraint expression is the same for all ensemble members
        path_constraints_function = ca.Function(
            'path_constraints',
            self.__func_orig_inputs,
            [path_constraint_expressions],
            function_options)
        path_constraints_function = path_constraints_function.expand()

        # Initialize a Function for the delayed feedback
        delayed_feedback_function = ca.Function(
            'delayed_feedback',
            self.__func_orig_inputs,
            delayed_feedback_expressions,
            function_options)
        delayed_feedback_function = delayed_feedback_function.expand()

        # Set up accumulation over time (integration, and generation of
        # collocation constraints)
        if len(integrated_variables) > 0:
            accumulated_X = ca.MX.sym('accumulated_X', len(integrated_variables))
        else:
            accumulated_X = ca.MX.sym('accumulated_X', 0)

        path_variables_size = sum(x.size1() for x in self.path_variables)
        extra_constant_inputs_size = sum(x.size1() for x in self.__extra_constant_inputs)

        accumulated_U = ca.MX.sym(
            'accumulated_U',
            (2 * (len(collocated_variables) +
             len(self.dae_variables['constant_inputs']) + 1) +
             path_variables_size +
             extra_constant_inputs_size))

        integrated_states_0 = accumulated_X[0:len(integrated_variables)]
        integrated_states_1 = ca.MX.sym(
            'integrated_states_1', len(integrated_variables))
        collocated_states_0 = accumulated_U[0:len(collocated_variables)]
        collocated_states_1 = accumulated_U[
            len(collocated_variables):2 * len(collocated_variables)]
        constant_inputs_0 = accumulated_U[2 * len(collocated_variables):2 * len(
            collocated_variables) + len(self.dae_variables['constant_inputs'])]
        constant_inputs_1 = accumulated_U[2 * len(collocated_variables) + len(self.dae_variables[
            'constant_inputs']):2 * len(collocated_variables) + 2 * len(self.dae_variables['constant_inputs'])]

        offset = 2 * (len(collocated_variables) + len(self.dae_variables['constant_inputs']))
        collocation_time_0 = accumulated_U[offset + 0]
        collocation_time_1 = accumulated_U[offset + 1]
        path_variables_1 = accumulated_U[offset + 2:offset + 2 + len(self.path_variables)]
        extra_constant_inputs_1 = accumulated_U[offset + 2 + len(self.path_variables):]

        # Approximate derivatives using backwards finite differences
        dt = collocation_time_1 - collocation_time_0
        collocated_finite_differences = (
            collocated_states_1 - collocated_states_0) / dt

        # We use ca.vertcat to compose the list into an MX.  This is, in
        # CasADi 2.4, faster.
        accumulated_Y = []

        # Integrate integrated states
        if len(integrated_variables) > 0:
            # Perform step by computing implicit function
            # CasADi shares subexpressions that are bundled into the same Function.
            # The first argument is the guess for the new value of
            # integrated_states.
            [integrated_states_1] = integrator_step_function.call(
                [integrated_states_0,
                 integrated_states_0,
                 symbolic_parameters,
                 ca.vertcat(
                    collocated_states_0,
                    constant_inputs_0,
                    dt,
                    collocated_states_1,
                    collocated_finite_differences,
                    constant_inputs_1,
                    collocation_time_1 - t0)],
                False, True)
            accumulated_Y.append(integrated_states_1)

            # Recompute finite differences with computed new state, for use in the collocation part below
            # We don't use substititute() for this, as it becomes expensive
            # over long integration horizons.
            if len(collocated_variables) > 0:
                integrated_finite_differences = (
                    integrated_states_1 - integrated_states_0) / dt
        else:
            integrated_finite_differences = ca.MX()

        # Call DAE residual at collocation point
        # Time stamp following paragraph 3.6.7 of the Modelica
        # specifications, version 3.3.
        if len(collocated_variables) > 0:
            if theta < 1:
                # Obtain state vector
                [dae_residual_0] = dae_residual_function_collocated.call(
                    [symbolic_parameters,
                     ca.vertcat(
                        integrated_states_0,
                        collocated_states_0,
                        integrated_finite_differences,
                        collocated_finite_differences,
                        constant_inputs_0,
                        collocation_time_0 - t0)],
                    False, True)
            if theta > 0:
                # Obtain state vector
                [dae_residual_1] = dae_residual_function_collocated.call(
                    [symbolic_parameters,
                     ca.vertcat(
                        integrated_states_1,
                        collocated_states_1,
                        integrated_finite_differences,
                        collocated_finite_differences,
                        constant_inputs_1,
                        collocation_time_1 - t0)],
                    False, True)
            if theta == 0:
                accumulated_Y.append(dae_residual_0)
            elif theta == 1:
                accumulated_Y.append(dae_residual_1)
            else:
                accumulated_Y.append(
                    (1 - theta) * dae_residual_0 + theta * dae_residual_1)

        self.__func_inputs_implicit = [
            symbolic_parameters,
            ca.vertcat(
                integrated_states_1,
                collocated_states_1,
                integrated_finite_differences,
                collocated_finite_differences,
                constant_inputs_1,
                collocation_time_1 - t0,
                path_variables_1,
                extra_constant_inputs_1),
            symbolic_extra_variables]

        accumulated_Y.extend(path_objective_function.call(
            self.__func_inputs_implicit, False, True))

        accumulated_Y.extend(path_constraints_function.call(
            self.__func_inputs_implicit, False, True))

        accumulated_Y.extend(delayed_feedback_function.call(
            self.__func_inputs_implicit, False, True))

        # Save the accumulated inputs such that can be used later in map_path_expression()
        self.__func_accumulated_inputs = (
            accumulated_X, accumulated_U,
            ca.veccat(symbolic_parameters, symbolic_extra_variables))

        # Use map/mapaccum to capture integration and collocation constraint generation over the
        # entire time horizon with one symbolic operation. This saves a lot of memory.
        if len(integrated_variables) > 0:
            accumulated = ca.Function(
                'accumulated',
                self.__func_accumulated_inputs,
                [accumulated_Y[0], ca.vertcat(*accumulated_Y[1:])],
                function_options)
            accumulation = accumulated.mapaccum('accumulation', n_collocation_times - 1)
        else:
            # Fully collocated problem. Use map(), so that we can use
            # parallelization along the time axis.
            accumulated = ca.Function(
                'accumulated',
                self.__func_accumulated_inputs,
                [ca.vertcat(*accumulated_Y)],
                function_options)
            accumulation = accumulated.map(n_collocation_times - 1, 'openmp')

        # Start collecting constraints
        f = []
        g = []
        lbg = []
        ubg = []

        # Add constraints for initial conditions
        if self.__initial_residual_with_params_fun_map is None:
            initial_residual_with_params_fun = ca.Function(
                'initial_residual_total',
                [symbolic_parameters,
                 ca.vertcat(*(
                    self.dae_variables['states'] +
                    self.dae_variables['algebraics'] +
                    self.dae_variables['control_inputs'] +
                    integrated_derivatives +
                    collocated_derivatives +
                    self.dae_variables['constant_inputs'] +
                    self.dae_variables['time']))],
                [ca.veccat(dae_residual, initial_residual)],
                function_options)
            self.__initial_residual_with_params_fun_map = initial_residual_with_params_fun.map(
                self.ensemble_size)
        initial_residual_with_params_fun_map = self.__initial_residual_with_params_fun_map
        [res] = initial_residual_with_params_fun_map.call(
            [ensemble_aggregate["parameters"],
             ca.vertcat(*[
                ensemble_aggregate["initial_state"],
                ensemble_aggregate["initial_derivatives"],
                ensemble_aggregate["initial_constant_inputs"],
                ca.repmat([0.0], 1, self.ensemble_size)])],
            False, True)

        res = ca.vec(res)
        g.append(res)
        zeros = [0.0] * res.size1()
        lbg.extend(zeros)
        ubg.extend(zeros)

        # The initial values and the interpolated mapped arguments are saved
        # such that can be reused in map_path_expression().
        self.__func_mapped_inputs = []
        self.__func_initial_inputs = []

        # Process the objectives and constraints for each ensemble member separately.
        # Note that we don't use map here for the moment, so as to allow each ensemble member to define its own
        # constraints and objectives.  Path constraints are applied for all ensemble members simultaneously
        # at the moment.  We can get rid of map again, and allow every ensemble member to specify its own
        # path constraints as well, once CasADi has some kind of loop detection.
        for ensemble_member in range(self.ensemble_size):
            logger.info(
                "Transcribing ensemble member {}/{}".format(ensemble_member + 1, self.ensemble_size))

            initial_state = ensemble_aggregate["initial_state"][:, ensemble_member]
            initial_derivatives = ensemble_aggregate["initial_derivatives"][:, ensemble_member]
            initial_path_variables = ensemble_aggregate["initial_path_variables"][:, ensemble_member]
            initial_constant_inputs = ensemble_aggregate["initial_constant_inputs"][:, ensemble_member]
            initial_extra_constant_inputs = ensemble_aggregate["initial_extra_constant_inputs"][:, ensemble_member]
            parameters = ensemble_aggregate["parameters"][:, ensemble_member]
            extra_variables = ca.vertcat(*[
                self.extra_variable(var.name(), ensemble_member) for var in self.extra_variables])

            constant_inputs = ensemble_store[ensemble_member]["constant_inputs"]
            extra_constant_inputs = ensemble_store[ensemble_member]["extra_constant_inputs"]

            # Initial conditions specified in history timeseries
            history = self.history(ensemble_member)
            for variable in itertools.chain(self.differentiated_states, self.algebraic_states, self.controls):
                try:
                    history_timeseries = history[variable]
                except KeyError:
                    pass
                else:
                    interpolation_method = self.interpolation_method(variable)
                    val = self.interpolate(
                        t0, history_timeseries.times, history_timeseries.values, np.nan, np.nan, interpolation_method)
                    val /= self.variable_nominal(variable)

                    if not np.isnan(val):
                        idx = self.__indices_as_lists[ensemble_member][variable][0]

                        if val < lbx[idx] or val > ubx[idx]:
                            logger.warning("Initial value {} for variable '{}' outside bounds.".format(val, variable))

                        lbx[idx] = ubx[idx] = val

            initial_derivative_constraints = []

            ensemble_member_size = int(self.__state_size / self.ensemble_size)

            der_offset = (self.__control_size
                          + (ensemble_member + 1) * ensemble_member_size
                          - len(self.dae_variables['derivatives']))

            for i, variable in enumerate(self.differentiated_states):
                try:
                    history_timeseries = history[variable]
                except KeyError:
                    pass
                else:
                    if len(history_timeseries.times) <= 1 or np.isnan(history_timeseries.values[-2]):
                        continue

                    assert history_timeseries.times[-1] == t0

                    if np.isnan(history_timeseries.values[-1]):
                        t0_val = self.state_vector(variable, ensemble_member=ensemble_member)[0]
                        t0_val *= self.variable_nominal(variable)

                        val = (t0_val - history_timeseries.values[-2]) / (t0 - history_timeseries.times[-2])
                        sym = initial_derivatives[i]
                        initial_derivative_constraints.append(sym - val)
                    else:
                        interpolation_method = self.interpolation_method(variable)

                        t0_val = self.interpolate(
                            t0,
                            history_timeseries.times,
                            history_timeseries.values,
                            np.nan,
                            np.nan,
                            interpolation_method
                        )
                        val = (t0_val - history_timeseries.values[-2]) / (t0 - history_timeseries.times[-2])
                        val *= self.__initial_dt[variable]
                        val /= self.variable_nominal(variable)

                        idx = der_offset + i
                        lbx[idx] = ubx[idx] = val

            if len(initial_derivative_constraints) > 0:
                g.append(ca.vertcat(*initial_derivative_constraints))
                lbg.append(np.zeros(len(initial_derivative_constraints)))
                ubg.append(np.zeros(len(initial_derivative_constraints)))

            # Initial conditions for integrator
            accumulation_X0 = []
            for variable in self.integrated_states:
                value = self.state_vector(
                    variable, ensemble_member=ensemble_member)[0]
                nominal = self.variable_nominal(variable)
                if nominal != 1:
                    value *= nominal
                accumulation_X0.append(value)
            # if len(self.integrated_states) > 0:
            #    accumulation_X0.extend(
            #        [0.0] * (dae_residual_collocated_size + 1))
            accumulation_X0 = ca.vertcat(*accumulation_X0)

            # Input for map
            logger.info("Interpolating states")

            accumulation_U = [None] * (
                1 + 2 * len(self.dae_variables['constant_inputs']) + 3
                + len(self.__extra_constant_inputs))

            # Most variables have collocation times equal to the global
            # collocation times. Use a vectorized approach to process them.
            interpolated_states_explicit = []
            interpolated_states_implicit = []

            place_holder = [-1] * n_collocation_times
            for variable in collocated_variable_names:
                var_inds = self.__indices_as_lists[ensemble_member][variable]

                # If the variable times != collocation times, what we do here is just a placeholder
                if len(var_inds) != n_collocation_times:
                    var_inds = var_inds.copy()
                    var_inds.extend(place_holder)
                    var_inds = var_inds[:n_collocation_times]

                interpolated_states_explicit.extend(var_inds[:-1])
                interpolated_states_implicit.extend(var_inds[1:])

            repeated_nominals = np.tile(np.repeat(collocated_variable_nominals, n_collocation_times - 1), 2)
            interpolated_states = ca.vertcat(X[interpolated_states_explicit],
                                             X[interpolated_states_implicit]) * repeated_nominals
            interpolated_states = interpolated_states.reshape((n_collocation_times - 1, len(collocated_variables)*2))

            # Handle variables that have different collocation times.
            for j, variable in enumerate(collocated_variable_names):
                times = self.times(variable)
                if n_collocation_times == len(times):
                    # Already handled
                    continue

                interpolation_method = self.interpolation_method(variable)
                values = self.state_vector(variable, ensemble_member=ensemble_member)
                interpolated = interpolate(
                    times, values, collocation_times, False, interpolation_method)

                nominal = self.variable_nominal(variable)
                if nominal != 1:
                    interpolated *= nominal

                interpolated_states[:, j] = interpolated[:-1]
                interpolated_states[:, len(collocated_variables) + j] = interpolated[1:]

            # We do not cache the Jacobians, as the structure may change from ensemble member to member,
            # and from goal programming/homotopy run to run.
            # We could, of course, pick the states apart into controls and states,
            # and generate Jacobians for each set separately and for each ensemble member separately, but
            # in this case the increased complexity may well offset the performance gained by caching.
            accumulation_U[0] = reduce_matvec(interpolated_states, self.solver_input)

            for j, variable in enumerate(self.dae_variables['constant_inputs']):
                variable = variable.name()
                constant_input = constant_inputs[variable]
                accumulation_U[
                    1 + j] = ca.MX(constant_input[0:n_collocation_times - 1])
                accumulation_U[1 + len(self.dae_variables[
                    'constant_inputs']) + j] = ca.MX(constant_input[1:n_collocation_times])

            accumulation_U[1 + 2 * len(self.dae_variables[
                'constant_inputs'])] = ca.MX(collocation_times[0:n_collocation_times - 1])
            accumulation_U[1 + 2 * len(self.dae_variables[
                'constant_inputs']) + 1] = ca.MX(collocation_times[1:n_collocation_times])

            path_variables = [None] * len(self.path_variables)
            for j, variable in enumerate(self.__path_variable_names):
                variable_size = self.__variable_sizes[variable]
                values = self.state_vector(
                    variable, ensemble_member=ensemble_member)

                nominal = self.variable_nominal(variable)
                if isinstance(nominal, np.ndarray):
                    nominal = np.broadcast_to(nominal, (n_collocation_times, variable_size)).transpose().ravel()
                    values *= nominal
                elif nominal != 1:
                    values *= nominal

                path_variables[j] = values.reshape((n_collocation_times, variable_size))[1:, :]

            accumulation_U[1 + 2 * len(
                self.dae_variables['constant_inputs']) + 2] = reduce_matvec(
                    ca.horzcat(*path_variables), self.solver_input)

            for j, variable in enumerate(self.__extra_constant_inputs):
                variable = variable.name()
                constant_input = extra_constant_inputs[variable]
                accumulation_U[1 + 2 * len(self.dae_variables['constant_inputs']) + 3 + j] = \
                    ca.MX(constant_input[1:n_collocation_times, :])

            # Construct matrix using O(states) CasADi operations
            # This is faster than using blockcat, presumably because of the
            # row-wise scaling operations.
            logger.info("Aggregating and de-scaling variables")

            accumulation_U = ca.transpose(ca.horzcat(*accumulation_U))

            # Map to all time steps
            logger.info("Mapping")

            # Save these inputs such that can be used later in map_path_expression()
            self.__func_mapped_inputs.append(
                (accumulation_X0, accumulation_U,
                 ca.repmat(ca.vertcat(parameters, extra_variables), 1, n_collocation_times - 1)))

            self.__func_initial_inputs.append([parameters, ca.vertcat(
                        initial_state, initial_derivatives, initial_constant_inputs, 0.0,
                        initial_path_variables, initial_extra_constant_inputs),
                        extra_variables])

            integrators_and_collocation_and_path_constraints = accumulation(
                *self.__func_mapped_inputs[ensemble_member])

            if len(integrated_variables) > 0:
                integrators = integrators_and_collocation_and_path_constraints[0]
                integrators_and_collocation_and_path_constraints = integrators_and_collocation_and_path_constraints[1]
            if integrators_and_collocation_and_path_constraints.numel() > 0:
                collocation_constraints = ca.vec(integrators_and_collocation_and_path_constraints[
                    :dae_residual_collocated_size,
                    0:n_collocation_times - 1])
                discretized_path_objective = ca.vec(integrators_and_collocation_and_path_constraints[
                    dae_residual_collocated_size:dae_residual_collocated_size + path_objective.size1(),
                    0:n_collocation_times - 1])
                discretized_path_constraints = ca.vec(integrators_and_collocation_and_path_constraints[
                    dae_residual_collocated_size + path_objective.size1():dae_residual_collocated_size +
                    path_objective.size1() + path_constraint_expressions.size1(),
                    0:n_collocation_times - 1])
                discretized_delayed_feedback = integrators_and_collocation_and_path_constraints[
                    dae_residual_collocated_size + path_objective.size1() + path_constraint_expressions.size1():,
                    0:n_collocation_times - 1]
            else:
                collocation_constraints = ca.MX()
                discretized_path_objective = ca.MX()
                discretized_path_constraints = ca.MX()
                discretized_delayed_feedback = ca.MX()

            logger.info("Composing NLP segment")

            # Store integrators for result extraction
            if len(integrated_variables) > 0:
                self.integrators = {}
                for i, variable in enumerate(integrated_variables):
                    self.integrators[variable.name()] = integrators[i, :]
                self.integrators_mx = []
                for j in range(integrators.size2()):
                    self.integrators_mx.append(integrators[:, j])

            # Add collocation constraints
            if collocation_constraints.size1() > 0:
                g.append(collocation_constraints)
                zeros = np.zeros(collocation_constraints.size1())
                lbg.extend(zeros)
                ubg.extend(zeros)

            # Delayed feedback
            # Make an array of all unique times in history series
            history_times = np.unique([history_series.times for history_series in history.values()])
            # By convention, the last timestep in history series is the initial time. We drop this index
            history_times = history_times[:-1]

            # Find the historical values of states, extrapolating backward if necessary
            history_values = np.empty((history_times.shape[0], len(integrated_variables) + len(collocated_variables)))
            if history_times.shape[0] > 0:
                for j, var in enumerate(integrated_variables + collocated_variables):
                    var_name = var.name()
                    try:
                        history_series = history[var_name]
                    except KeyError:
                        history_values[:, j] = np.nan
                    else:
                        interpolation_method = self.interpolation_method(var_name)
                        history_values[:, j] = self.interpolate(
                            history_times,
                            history_series.times,
                            history_series.values,
                            np.nan,
                            np.nan,
                            interpolation_method)

            # Calculate the historical derivatives of historical values
            history_derivatives = ca.repmat(np.nan, 1, history_values.shape[1])
            if history_times.shape[0] > 1:
                history_derivatives = ca.vertcat(
                    history_derivatives,
                    np.diff(history_values, axis=0) / np.diff(history_times)[:, None])

            # Find the historical values of constant inputs, extrapolating backward if necessary
            constant_input_values = np.empty((history_times.shape[0], len(self.dae_variables['constant_inputs'])))
            if history_times.shape[0] > 0:
                for j, var in enumerate(self.dae_variables['constant_inputs']):
                    var_name = var.name()
                    try:
                        constant_input_series = raw_constant_inputs[var_name]
                    except KeyError:
                        constant_input_values[:, j] = np.nan
                    else:
                        interpolation_method = self.interpolation_method(var_name)
                        constant_input_values[:, j] = self.interpolate(
                            history_times,
                            constant_input_series.times,
                            constant_input_series.values,
                            np.nan,
                            np.nan,
                            interpolation_method)

            if len(delayed_feedback_expressions) > 0:
                delayed_feedback_history = np.zeros((history_times.shape[0], len(delayed_feedback_expressions)))
                for i, time in enumerate(history_times):
                    [history_delayed_feedback_res] = delayed_feedback_function.call(
                        [parameters, ca.veccat(
                            ca.transpose(history_values[i, :]),
                            ca.transpose(history_derivatives[i, :]),
                            ca.transpose(constant_input_values[i, :]),
                            time,
                            ca.repmat(np.nan, len(self.path_variables)),
                            ca.repmat(np.nan, len(self.__extra_constant_inputs))),
                         ca.repmat(np.nan, len(self.extra_variables))])
                    delayed_feedback_history[i, :] = history_delayed_feedback_res

                initial_delayed_feedback = delayed_feedback_function.call(
                    self.__func_initial_inputs[ensemble_member], False, True)

                path_variables_nominal = np.ones(path_variables_size)
                offset = 0
                for variable in self.__path_variable_names:
                    variable_size = self.__variable_sizes[variable]
                    path_variables_nominal[offset:offset + variable_size] = self.variable_nominal(variable)
                    offset += variable_size

                nominal_delayed_feedback = delayed_feedback_function.call(
                    [parameters, ca.vertcat(
                        [self.variable_nominal(var.name()) for var in integrated_variables + collocated_variables],
                        np.zeros((initial_derivatives.size1(), 1)),
                        initial_constant_inputs,
                        0.0,
                        path_variables_nominal,
                        initial_extra_constant_inputs), extra_variables])

            if delayed_feedback_expressions:
                # Resolve delay values
                # First, substitute parameters for values all at once. Make
                # sure substitute() gets called with the right signature. This
                # means we need at least one element that is of type MX.
                delayed_feedback_durations = list(delayed_feedback_durations)
                delayed_feedback_durations[0] = ca.MX(delayed_feedback_durations[0])

                substituted_delay_durations = ca.substitute(
                    delayed_feedback_durations,
                    [ca.vertcat(symbolic_parameters)],
                    [ca.vertcat(parameters)])

                # Use mapped function to evaluate delay in terms of constant inputs
                mapped_delay_function = ca.Function(
                    'delay_values',
                    self.dae_variables['time'] + self.dae_variables['constant_inputs'],
                    substituted_delay_durations
                    ).map(len(collocation_times))

                # Call mapped delay function with inputs as arrays
                evaluated_delay_durations = mapped_delay_function.call(
                    [collocation_times] +
                    [constant_inputs[v.name()] for v in self.dae_variables['constant_inputs']])

                for i in range(len(delayed_feedback_expressions)):
                    in_variable_name = delayed_feedback_states[i]
                    expression = delayed_feedback_expressions[i]
                    delay = evaluated_delay_durations[i]

                    # Resolve aliases
                    in_canonical, in_sign = self.alias_relation.canonical_signed(
                        in_variable_name)
                    in_times = self.times(in_canonical)
                    in_nominal = self.variable_nominal(in_canonical)
                    in_values = in_nominal * \
                        self.state_vector(
                            in_canonical, ensemble_member=ensemble_member)
                    if in_sign < 0:
                        in_values *= in_sign

                    # Cast delay from DM to np.array
                    delay = delay.toarray().flatten()

                    assert np.all(np.isfinite(delay)), (
                        'Delay duration must be resolvable to real values at transcribe()')

                    out_times = np.concatenate([history_times, collocation_times])
                    out_values = ca.veccat(
                        delayed_feedback_history[:, i],
                        initial_delayed_feedback[i],
                        ca.transpose(discretized_delayed_feedback[i, :]))

                    # Check whether enough history has been specified, and that no
                    # needed history values are missing
                    hist_earliest = np.min(collocation_times - delay)
                    hist_start_ind = np.searchsorted(out_times, hist_earliest)
                    if out_times[hist_start_ind] != hist_earliest:
                        # We need an earlier value to interpolate with
                        hist_start_ind -= 1

                    if np.any(np.isnan(delayed_feedback_history[hist_start_ind:, i])):
                        logger.warning(
                            'Incomplete history for delayed expression {}. '
                            'Extrapolating t0 value backwards in time.'.format(
                                expression))
                        out_times = out_times[len(history_times):]
                        out_values = out_values[len(history_times):]

                    # Set up delay constraints
                    if len(collocation_times) != len(in_times):
                        interpolation_method = self.interpolation_method(
                            in_canonical)
                        x_in = interpolate(in_times, in_values,
                                           collocation_times, False, interpolation_method)
                    else:
                        x_in = in_values
                    interpolation_method = self.interpolation_method(in_canonical)
                    x_out_delayed = interpolate(
                        out_times, out_values, collocation_times - delay, False, interpolation_method)

                    nominal = nominal_delayed_feedback[i]

                    g.append((x_in - x_out_delayed) / nominal)
                    zeros = np.zeros(n_collocation_times)
                    lbg.extend(zeros)
                    ubg.extend(zeros)

            # Objective
            f_member = self.objective(ensemble_member)
            if f_member.size1() == 0:
                f_member = 0
            if path_objective.size1() > 0:
                initial_path_objective = path_objective_function.call(
                    self.__func_initial_inputs[ensemble_member], False, True)
                f_member += initial_path_objective[0] + \
                    ca.sum1(discretized_path_objective)
            f.append(self.ensemble_member_probability(
                ensemble_member) * f_member)

            if logger.getEffectiveLevel() == logging.DEBUG:
                logger.debug(
                    "Adding objective {}".format(f_member))

            # Constraints
            constraints = self.constraints(ensemble_member)
            if logger.getEffectiveLevel() == logging.DEBUG:
                for constraint in constraints:
                    logger.debug(
                        "Adding constraint {}, {}, {}".format(*constraint))

            if constraints:
                g_constraint, lbg_constraint, ubg_constraint = list(zip(*constraints))

                lbg_constraint = list(lbg_constraint)
                ubg_constraint = list(ubg_constraint)

                # Broadcast lbg/ubg if it's a vector constraint
                for i, (g_i, lbg_i, ubg_i) in enumerate(zip(g_constraint, lbg_constraint, ubg_constraint)):
                    s = g_i.size1()
                    if s > 1:
                        if not isinstance(lbg_i, np.ndarray) or lbg_i.shape[0] == 1:
                            lbg_constraint[i] = np.full(s, lbg_i)
                        elif lbg_i.shape[0] != g_i.shape[0]:
                            raise Exception("Shape mismatch between constraint #{} ({},) and its lower bound ({},)"
                                            .format(i, g_i.shape[0], lbg_i.shape[0]))

                        if not isinstance(ubg_i, np.ndarray) or ubg_i.shape[0] == 1:
                            ubg_constraint[i] = np.full(s, ubg_i)
                        elif ubg_i.shape[0] != g_i.shape[0]:
                            raise Exception("Shape mismatch between constraint #{} ({},) and its upper bound ({},)"
                                            .format(i, g_i.shape[0], ubg_i.shape[0]))

                g.extend(g_constraint)
                lbg.extend(lbg_constraint)
                ubg.extend(ubg_constraint)

            # Path constraints
            # We need to call self.path_constraints() again here,
            # as the bounds may change from ensemble member to member.
            if ensemble_member > 0:
                path_constraints = self.path_constraints(ensemble_member)

            if len(path_constraints) > 0:
                # We need to evaluate the path constraints at t0, as the initial time is not
                # included in the accumulation.
                [initial_path_constraints] = path_constraints_function.call(
                    self.__func_initial_inputs[ensemble_member], False, True)
                g.append(initial_path_constraints)
                g.append(discretized_path_constraints)

                lbg_path_constraints = np.empty(
                    (path_constraint_expressions.size1(), n_collocation_times))
                ubg_path_constraints = np.empty(
                    (path_constraint_expressions.size1(), n_collocation_times))

                j = 0
                for path_constraint in path_constraints:
                    if logger.getEffectiveLevel() == logging.DEBUG:
                        logger.debug(
                            "Adding path constraint {}, {}, {}".format(*path_constraint))

                    s = path_constraint[0].size1()

                    lb = path_constraint[1]
                    if isinstance(lb, ca.MX) and not lb.is_constant():
                        [lb] = ca.substitute(
                            [lb], symbolic_parameters, self.__parameter_values_ensemble_member_0)
                    elif isinstance(lb, Timeseries):
                        lb = self.interpolate(
                            collocation_times, lb.times, lb.values, -np.inf, -np.inf).transpose()
                    elif isinstance(lb, np.ndarray):
                        lb = np.broadcast_to(lb, (n_collocation_times, s)).transpose()

                    ub = path_constraint[2]
                    if isinstance(ub, ca.MX) and not ub.is_constant():
                        [ub] = ca.substitute(
                            [ub], symbolic_parameters, self.__parameter_values_ensemble_member_0)
                    elif isinstance(ub, Timeseries):
                        ub = self.interpolate(
                            collocation_times, ub.times, ub.values, np.inf, np.inf).transpose()
                    elif isinstance(ub, np.ndarray):
                        ub = np.broadcast_to(ub, (n_collocation_times, s)).transpose()

                    lbg_path_constraints[j:j+s, :] = lb
                    ubg_path_constraints[j:j+s, :] = ub

                    j += s

                lbg.extend(lbg_path_constraints.transpose().ravel())
                ubg.extend(ubg_path_constraints.transpose().ravel())

        # NLP function
        logger.info("Creating NLP dictionary")

        nlp = {'x': X, 'f': ca.sum1(ca.vertcat(*f)), 'g': ca.vertcat(*g)}

        # Done
        logger.info("Done transcribing problem")

        return discrete, lbx, ubx, lbg, ubg, x0, nlp

    def clear_transcription_cache(self):
        """
        Clears the DAE ``Function``s that were cached by ``transcribe``.
        """
        self.__dae_residual_function_collocated = None
        self.__integrator_step_function = None
        self.__initial_residual_with_params_fun_map = None

    def extract_results(self, ensemble_member=0):
        logger.info("Extracting results")

        # Gather results in a dictionary
        control_results = self.extract_controls(ensemble_member)
        state_results = self.extract_states(ensemble_member)

        # Merge dictionaries
        results = AliasDict(self.alias_relation)
        results.update(control_results)
        results.update(state_results)

        logger.info("Done extracting results")

        # Return results dictionary
        return results

    @property
    def solver_input(self):
        return self.__solver_input

    def solver_options(self):
        options = super(CollocatedIntegratedOptimizationProblem,
                        self).solver_options()

        solver = options['solver']
        assert solver in ['bonmin', 'ipopt']

        # Set the option in both cases, to avoid one inadvertently remaining in the cache.
        options[solver]['jac_c_constant'] = 'yes' if self.linear_collocation else 'no'
        return options

    def integrator_options(self):
        """
        Configures the implicit function used for time step integration.

        :returns: A dictionary of CasADi :class:`rootfinder` options.  See the CasADi documentation for details.
        """
        return {}

    @property
    def controls(self):
        return self.__controls

    def discretize_controls(self, bounds):
        # Default implementation: One single set of control inputs for all
        # ensembles
        count = 0
        for variable in self.controls:
            times = self.times(variable)
            n_times = len(times)

            count += n_times

        # We assume the seed for the controls to be identical for the entire ensemble.
        # After all, we don't use a stochastic tree if we end up here.
        seed = self.seed(ensemble_member=0)

        indices = [{} for ensemble_member in range(self.ensemble_size)]

        discrete = np.zeros(count, dtype=np.bool)

        lbx = np.full(count, -np.inf, dtype=np.float64)
        ubx = np.full(count, np.inf, dtype=np.float64)

        x0 = np.zeros(count, dtype=np.float64)

        offset = 0
        for variable in self.controls:
            times = self.times(variable)
            n_times = len(times)

            for ensemble_member in range(self.ensemble_size):
                indices[ensemble_member][variable] = slice(offset, offset + n_times)

            discrete[offset:offset +
                     n_times] = self.variable_is_discrete(variable)

            try:
                bound = bounds[variable]
            except KeyError:
                pass
            else:
                nominal = self.variable_nominal(variable)
                interpolation_method = self.interpolation_method(variable)
                if bound[0] is not None:
                    if isinstance(bound[0], Timeseries):
                        lbx[offset:offset + n_times] = self.interpolate(
                            times,
                            bound[0].times,
                            bound[0].values,
                            -np.inf,
                            -np.inf,
                            interpolation_method) / nominal
                    else:
                        lbx[offset:offset + n_times] = bound[0] / nominal
                if bound[1] is not None:
                    if isinstance(bound[1], Timeseries):
                        ubx[offset:offset + n_times] = self.interpolate(
                            times,
                            bound[1].times,
                            bound[1].values,
                            +np.inf,
                            +np.inf,
                            interpolation_method) / nominal
                    else:
                        ubx[offset:offset + n_times] = bound[1] / nominal

                try:
                    seed_k = seed[variable]
                    x0[offset:offset + n_times] = self.interpolate(
                        times,
                        seed_k.times,
                        seed_k.values,
                        0,
                        0,
                        interpolation_method) / nominal
                except KeyError:
                    pass

            offset += n_times

        # Return number of control variables
        return count, discrete, lbx, ubx, x0, indices

    def extract_controls(self, ensemble_member=0):
        # Solver output
        X = self.solver_output.copy()

        # Extract control inputs
        results = {}
        offset = 0
        for variable in self.controls:
            n_times = len(self.times(variable))
            results[variable] = self.variable_nominal(
                variable) * X[offset:offset + n_times]
            offset += n_times

        # Done
        return results

    def control_at(self, variable, t, ensemble_member=0, scaled=False, extrapolate=True):
        # Default implementation: One single set of control inputs for all
        # ensembles
        t0 = self.initial_time
        X = self.solver_input

        canonical, sign = self.alias_relation.canonical_signed(variable)
        offset = 0
        for control_input in self.controls:
            times = self.times(control_input)
            if control_input == canonical:
                nominal = self.variable_nominal(control_input)
                n_times = len(times)
                variable_values = X[offset:offset + n_times]
                f_left, f_right = np.nan, np.nan
                if t < t0:
                    history = self.history(ensemble_member)
                    try:
                        history_timeseries = history[control_input]
                    except KeyError:
                        if extrapolate:
                            sym = variable_values[0]
                        else:
                            sym = np.nan
                    else:
                        if extrapolate:
                            f_left = history_timeseries.values[0]
                            f_right = history_timeseries.values[-1]
                        interpolation_method = self.interpolation_method(control_input)
                        sym = self.interpolate(
                            t,
                            history_timeseries.times,
                            history_timeseries.values,
                            f_left,
                            f_right,
                            interpolation_method)
                    if not scaled and nominal != 1:
                        sym *= nominal
                else:
                    if not extrapolate and (t < times[0] or t > times[-1]):
                        raise Exception("Cannot interpolate for {}: Point {} outside of range [{}, {}]".format(
                            control_input, t, times[0], times[-1]))

                    interpolation_method = self.interpolation_method(control_input)
                    sym = interpolate(
                        times, variable_values, [t], False, interpolation_method)
                    if not scaled and nominal != 1:
                        sym *= nominal
                if sign < 0:
                    sym *= -1
                return sym
            offset += len(times)

        raise KeyError(variable)

    @property
    def differentiated_states(self):
        return self.__differentiated_states

    @property
    def algebraic_states(self):
        return self.__algebraic_states

    def discretize_states(self, bounds):
        # Default implementation: States for all ensemble members
        ensemble_member_size = 0

        variable_sizes = self.__variable_sizes

        # Space for collocated states
        for variable in itertools.chain(self.differentiated_states, self.algebraic_states, self.__path_variable_names):
            if variable in self.integrated_states:
                ensemble_member_size += 1  # Initial state
            else:
                ensemble_member_size += variable_sizes[variable] * len(self.times(variable))

        # Space for extra variables
        for variable in self.__extra_variable_names:
            ensemble_member_size += variable_sizes[variable]

        # Space for initial states and derivatives
        ensemble_member_size += len(self.dae_variables['derivatives'])

        # Total space requirement
        count = self.ensemble_size * ensemble_member_size

        # Allocate arrays
        indices = [{} for ensemble_member in range(self.ensemble_size)]

        discrete = np.zeros(count, dtype=np.bool)

        lbx = -np.inf * np.ones(count)
        ubx = np.inf * np.ones(count)

        x0 = np.zeros(count)

        # Indices
        for ensemble_member in range(self.ensemble_size):
            offset = ensemble_member * ensemble_member_size
            for variable in itertools.chain(
                    self.differentiated_states, self.algebraic_states, self.__path_variable_names):

                variable_size = variable_sizes[variable]

                if variable in self.integrated_states:
                    assert variable_size == 1
                    indices[ensemble_member][variable] = offset

                    offset += 1
                else:
                    times = self.times(variable)
                    n_times = len(times)

                    indices[ensemble_member][variable] = slice(offset, offset + n_times * variable_size)

                    offset += n_times * variable_size

            for extra_variable in self.__extra_variable_names:
                variable_size = variable_sizes[extra_variable]

                indices[ensemble_member][extra_variable] = slice(offset, offset + variable_size)

                offset += variable_size

        # Types
        for ensemble_member in range(self.ensemble_size):
            offset = ensemble_member * ensemble_member_size
            for variable in itertools.chain(
                    self.differentiated_states, self.algebraic_states, self.__path_variable_names):

                variable_size = variable_sizes[variable]

                if variable in self.integrated_states:
                    assert variable_size == 1
                    discrete[offset] = self.variable_is_discrete(variable)

                    offset += 1

                else:
                    times = self.times(variable)
                    n_times = len(times)

                    discrete[offset:offset +
                             n_times * variable_size] = self.variable_is_discrete(variable)

                    offset += n_times * variable_size

            for variable in self.__extra_variable_names:
                variable_size = variable_sizes[variable]

                discrete[
                    offset:offset + variable_size] = self.variable_is_discrete(variable)

                offset += variable_size

        # Bounds, defaulting to +/- inf, if not set
        for ensemble_member in range(self.ensemble_size):
            offset = ensemble_member * ensemble_member_size
            for variable in itertools.chain(
                    self.differentiated_states, self.algebraic_states, self.__path_variable_names):

                variable_size = variable_sizes[variable]

                if variable in self.integrated_states:
                    assert variable_size == 1
                    try:
                        bound = bounds[variable]
                    except KeyError:
                        pass
                    else:
                        nominal = self.variable_nominal(variable)
                        interpolation_method = self.interpolation_method(variable)
                        if bound[0] is not None:
                            if isinstance(bound[0], Timeseries):
                                lbx[offset] = self.interpolate(self.initial_time, bound[0].times, bound[
                                    0].values, -np.inf, -np.inf, interpolation_method) / nominal
                            else:
                                lbx[offset] = bound[0] / nominal
                        if bound[1] is not None:
                            if isinstance(bound[1], Timeseries):
                                ubx[offset] = self.interpolate(self.initial_time, bound[1].times, bound[
                                    1].values, +np.inf, +np.inf, interpolation_method) / nominal
                            else:
                                ubx[offset] = bound[1] / nominal

                    # Warn for NaNs
                    if np.any(np.isnan(lbx[offset])):
                        logger.error('Lower bound on variable {} contains NaN'.format(variable))
                    if np.any(np.isnan(ubx[offset])):
                        logger.error('Upper bound on variable {} contains NaN'.format(variable))

                    offset += 1

                else:
                    times = self.times(variable)
                    n_times = len(times)

                    try:
                        bound = bounds[variable]
                    except KeyError:
                        pass
                    else:
                        nominal = self.variable_nominal(variable)
                        interpolation_method = self.interpolation_method(variable)
                        if isinstance(nominal, np.ndarray):
                            nominal = np.broadcast_to(nominal, (n_times, variable_size)).transpose().ravel()

                        if bound[0] is not None:
                            if isinstance(bound[0], Timeseries):
                                lower_bound = self.interpolate(
                                    times,
                                    bound[0].times,
                                    bound[0].values,
                                    -np.inf,
                                    -np.inf,
                                    interpolation_method).ravel()
                            elif isinstance(bound[0], np.ndarray):
                                lower_bound = np.broadcast_to(bound[0], (n_times, variable_size)).transpose().ravel()
                            else:
                                lower_bound = bound[0]
                            lbx[offset:offset + variable_size * n_times] = lower_bound / nominal

                        if bound[1] is not None:
                            if isinstance(bound[1], Timeseries):
                                upper_bound = self.interpolate(
                                    times,
                                    bound[1].times,
                                    bound[1].values,
                                    +np.inf,
                                    +np.inf,
                                    interpolation_method).ravel()
                            elif isinstance(bound[1], np.ndarray):
                                upper_bound = np.broadcast_to(bound[1], (n_times, variable_size)).transpose().ravel()
                            else:
                                upper_bound = bound[1]
                            ubx[offset:offset + variable_size * n_times] = upper_bound / nominal

                    # Warn for NaNs
                    if np.any(np.isnan(lbx[offset:offset + n_times * variable_size])):
                        logger.error('Lower bound on variable {} contains NaN'.format(variable))
                    if np.any(np.isnan(ubx[offset:offset + n_times * variable_size])):
                        logger.error('Upper bound on variable {} contains NaN'.format(variable))

                    offset += n_times * variable_size

            for variable in self.__extra_variable_names:

                variable_size = variable_sizes[variable]

                try:
                    bound = bounds[variable]
                except KeyError:
                    pass
                else:
                    nominal = self.variable_nominal(variable)
                    if bound[0] is not None:
                        lbx[offset:offset + variable_size] = bound[0] / nominal
                    if bound[1] is not None:
                        ubx[offset:offset + variable_size] = bound[1] / nominal

                # Warn for NaNs
                if np.any(np.isnan(lbx[offset:offset + variable_size])):
                    logger.error('Lower bound on variable {} contains NaN'.format(variable))
                if np.any(np.isnan(ubx[offset:offset + variable_size])):
                    logger.error('Upper bound on variable {} contains NaN'.format(variable))

                offset += variable_size

            # Initial guess based on provided seeds, defaulting to zero if no
            # seed is given
            seed = self.seed(ensemble_member)

            offset = ensemble_member * ensemble_member_size
            for variable in itertools.chain(
                    self.differentiated_states, self.algebraic_states, self.__path_variable_names):

                variable_size = variable_sizes[variable]

                if variable in self.integrated_states:
                    assert variable_size == 1
                    try:
                        seed_k = seed[variable]
                        nominal = self.variable_nominal(variable)
                        interpolation_method = self.interpolation_method(variable)
                        x0[offset] = self.interpolate(
                            self.initial_time, seed_k.times, seed_k.values, 0, 0, interpolation_method) / nominal
                    except KeyError:
                        pass

                    offset += 1

                else:
                    times = self.times(variable)
                    n_times = len(times)

                    try:
                        seed_k = seed[variable]
                        nominal = self.variable_nominal(variable)
                        interpolation_method = self.interpolation_method(variable)
                        if isinstance(nominal, np.ndarray):
                            nominal = np.broadcast_to(nominal, (n_times, variable_size)).transpose().ravel()
                        x0[offset:offset + n_times * variable_size] = self.interpolate(
                            times,
                            seed_k.times,
                            seed_k.values,
                            0,
                            0,
                            interpolation_method).transpose().ravel() / nominal
                    except KeyError:
                        pass

                    offset += n_times * variable_size

            for variable in self.__extra_variable_names:

                variable_size = variable_sizes[variable]

                try:
                    seed_v = seed[variable]
                    nominal = self.variable_nominal(variable)
                    if isinstance(seed_v, np.ndarray):
                        seed_v = seed_v.ravel()
                    x0[offset:offset + variable_size] = seed_v / nominal
                except KeyError:
                    pass

                offset += variable_size

            for k, (state_name, der_name) in enumerate(
                    zip(self.__differentiated_states, self.__derivative_names)):
                try:
                    nominal = self.variable_nominal(state_name)
                    dt = self.__initial_dt[state_name]
                    x0[offset + k] = seed["initial_" + der_name] * dt / nominal
                except KeyError:
                    pass

        # Return number of state variables
        return count, discrete, lbx, ubx, x0, indices

    def extract_states(self, ensemble_member=0):
        # Solver output
        X = self.solver_output.copy()

        # Discretization parameters
        control_size = self.__control_size
        ensemble_member_size = int(self.__state_size / self.ensemble_size)

        # Extract control inputs
        results = {}

        # Perform integration, in order to extract integrated variables
        # We bundle all integrations into a single Function, so that subexpressions
        # are evaluated only once.
        if len(self.integrated_states) > 0:
            # Use integrators_mx to facilitate common subexpression
            # elimination.
            f = ca.Function('f', [self.solver_input], [
                ca.vertcat(*self.integrators_mx)])
            integrators_output = f(X)
            j = 0
            for variable in self.integrated_states:
                n = self.integrators[variable].size1()
                results[variable] = self.variable_nominal(
                    variable) * np.array(integrators_output[j:j + n, 0]).ravel()
                j += n

        # Extract collocated variables
        offset = control_size + ensemble_member * ensemble_member_size
        for variable in itertools.chain(self.differentiated_states, self.algebraic_states):
            if variable in self.integrated_states:
                offset += 1
            else:
                n_times = len(self.times(variable))
                results[variable] = self.variable_nominal(variable) * X[offset:offset + n_times]
                offset += n_times

        # Extract constant input aliases
        constant_inputs = self.constant_inputs(ensemble_member)
        for variable in self.dae_variables['constant_inputs']:
            variable = variable.name()
            try:
                constant_input = constant_inputs[variable]
            except KeyError:
                pass
            else:
                results[variable] = np.interp(self.times(
                    variable), constant_input.times, constant_input.values)

        variable_sizes = self.__variable_sizes

        # Extract path variables
        n_collocation_times = len(self.times())
        for variable in self.__path_variable_names:
            variable_size = variable_sizes[variable]

            if variable_size > 1:
                results[variable] = X[offset:offset + n_collocation_times * variable_size] \
                    .reshape((variable_size, n_collocation_times)).transpose()
            else:
                results[variable] = X[offset:offset + n_collocation_times]

            results[variable] *= self.variable_nominal(variable)
            offset += n_collocation_times * variable_size

        # Extract extra variables
        for variable in self.__extra_variable_names:
            variable_size = variable_sizes[variable]

            if variable_size > 1:
                # NOTE: To avoid confusion with 1D flat array for collocated variables, we
                # want to explicitly return a column vector here.
                results[variable] = X[offset:offset + variable_size][None, :]
                assert results[variable].ndim == 2
            else:
                results[variable] = X[offset].ravel()

            results[variable] *= self.variable_nominal(variable)
            offset += variable_size

        # Extract initial derivatives
        for k, (state_name, der_name) in enumerate(
                zip(self.__differentiated_states, self.__derivative_names)):
            try:
                nominal = self.variable_nominal(state_name)
                dt = self.__initial_dt[state_name]
                results["initial_" + der_name] = nominal / dt * X[offset + k].ravel()
            except KeyError:
                pass

        # Done
        return results

    def state_vector(self, variable, ensemble_member=0):
        indices = self.__indices[ensemble_member][variable]
        return self.solver_input[indices]

    def state_at(self, variable, t, ensemble_member=0, scaled=False, extrapolate=True):
        if isinstance(variable, ca.MX):
            variable = variable.name()

        if self.__variable_sizes.get(variable, 1) > 1:
            raise NotImplementedError("state_at() not supported for vector states")

        name = "{}[{},{}]{}".format(
            variable, ensemble_member, t - self.initial_time, 'S' if scaled else '')
        if extrapolate:
            name += 'E'
        try:
            return self.__symbol_cache[name]
        except KeyError:
            # Look up transcribe_problem() state.
            t0 = self.initial_time
            X = self.solver_input
            control_size = self.__control_size
            ensemble_member_size = int(self.__state_size / self.ensemble_size)

            # Fetch appropriate symbol, or value.
            canonical, sign = self.alias_relation.canonical_signed(variable)
            found = False
            if not found:
                offset = control_size + ensemble_member * ensemble_member_size
                for free_variable in itertools.chain(
                        self.differentiated_states, self.algebraic_states, self.__path_variable_names):
                    if free_variable == canonical:
                        times = self.times(free_variable)
                        n_times = len(times)
                        if free_variable in self.integrated_states:
                            nominal = 1
                            if t == self.initial_time:
                                sym = sign * X[offset]
                                found = True
                                break
                            else:
                                variable_values = ca.horzcat(sign * X[offset], self.integrators[
                                    free_variable]).T
                        else:
                            nominal = self.variable_nominal(free_variable)
                            variable_values = X[offset:offset + n_times]
                        f_left, f_right = np.nan, np.nan
                        if t < t0:
                            history = self.history(ensemble_member)
                            try:
                                history_timeseries = history[free_variable]
                            except KeyError:
                                if extrapolate:
                                    sym = variable_values[0]
                                else:
                                    sym = np.nan
                            else:
                                if extrapolate:
                                    f_left = history_timeseries.values[0]
                                    f_right = history_timeseries.values[-1]
                                interpolation_method = self.interpolation_method(free_variable)
                                sym = self.interpolate(
                                    t,
                                    history_timeseries.times,
                                    history_timeseries.values,
                                    f_left,
                                    f_right,
                                    interpolation_method)
                            if scaled and nominal != 1:
                                sym /= nominal
                        else:
                            if not extrapolate and (t < times[0] or t > times[-1]):
                                raise Exception("Cannot interpolate for {}: Point {} outside of range [{}, {}]".format(
                                    free_variable, t, times[0], times[-1]))

                            interpolation_method = self.interpolation_method(free_variable)
                            sym = interpolate(
                                times, variable_values, [t], False, interpolation_method)
                            if not scaled and nominal != 1:
                                sym *= nominal
                        if sign < 0:
                            sym *= -1
                        found = True
                        break
                    if free_variable in self.integrated_states:
                        offset += 1
                    else:
                        offset += len(self.times(free_variable))
            if not found:
                try:
                    sym = self.control_at(
                        variable, t, ensemble_member=ensemble_member, extrapolate=extrapolate)
                    found = True
                except KeyError:
                    pass
            if not found:
                constant_inputs = self.constant_inputs(ensemble_member)
                try:
                    constant_input = constant_inputs[variable]
                    found = True
                except KeyError:
                    pass
                else:
                    times = self.times(variable)
                    n_times = len(times)
                    f_left, f_right = np.nan, np.nan
                    if extrapolate:
                        f_left = constant_input.values[0]
                        f_right = constant_input.values[-1]
                    interpolation_method = self.interpolation_method(variable)
                    sym = self.interpolate(
                        t, constant_input.times, constant_input.values, f_left, f_right, interpolation_method)
            if not found:
                parameters = self.parameters(ensemble_member)
                try:
                    sym = parameters[variable]
                    found = True
                except KeyError:
                    pass
            if not found:
                raise KeyError(variable)

            # Cache symbol.
            self.__symbol_cache[name] = sym

            return sym

    def variable(self, variable):
        return self.__variables[variable]

    def extra_variable(self, extra_variable, ensemble_member=0):
        indices = self.__indices[ensemble_member][extra_variable]
        return self.solver_input[indices] * self.variable_nominal(extra_variable)

    def states_in(self, variable, t0=None, tf=None, ensemble_member=0):
        # Time stamps for this variable
        times = self.times(variable)

        # Set default values
        if t0 is None:
            t0 = times[0]
        if tf is None:
            tf = times[-1]

        # Find canonical variable
        canonical, sign = self.alias_relation.canonical_signed(variable)
        nominal = self.variable_nominal(canonical)
        state = nominal * self.state_vector(canonical, ensemble_member)
        if sign < 0:
            state *= -1

        # Compute combined points
        if t0 < times[0]:
            history = self.history(ensemble_member)
            try:
                history_timeseries = history[canonical]
            except KeyError:
                raise Exception(
                    "No history found for variable {}, but a historical value was requested".format(variable))
            else:
                history_times = history_timeseries.times[:-1]
                history = history_timeseries.values[:-1]
                if sign < 0:
                    history *= -1
        else:
            history_times = np.empty(0)
            history = np.empty(0)

        # Collect states within specified interval
        indices, = np.where(np.logical_and(times >= t0, times <= tf))
        history_indices, = np.where(np.logical_and(
            history_times >= t0, history_times <= tf))
        if (t0 not in times[indices]) and (t0 not in history_times[history_indices]):
            x0 = self.state_at(variable, t0, ensemble_member)
        else:
            x0 = ca.MX()
        if (tf not in times[indices]) and (tf not in history_times[history_indices]):
            xf = self.state_at(variable, tf, ensemble_member)
        else:
            xf = ca.MX()
        x = ca.vertcat(x0, history[history_indices],
                       state[indices[0]:indices[-1] + 1], xf)

        return x

    def integral(self, variable, t0=None, tf=None, ensemble_member=0):
        # Time stamps for this variable
        times = self.times(variable)

        # Set default values
        if t0 is None:
            t0 = times[0]
        if tf is None:
            tf = times[-1]

        # Find canonical variable
        canonical, sign = self.alias_relation.canonical_signed(variable)
        nominal = self.variable_nominal(canonical)
        state = nominal * self.state_vector(canonical, ensemble_member)
        if sign < 0:
            state *= -1

        # Compute combined points
        if t0 < times[0]:
            history = self.history(ensemble_member)
            try:
                history_timeseries = history[canonical]
            except KeyError:
                raise Exception(
                    "No history found for variable {}, but a historical value was requested".format(variable))
            else:
                history_times = history_timeseries.times[:-1]
                history = history_timeseries.values[:-1]
                if sign < 0:
                    history *= -1
        else:
            history_times = np.empty(0)
            history = np.empty(0)

        # Collect time stamps and states, "knots".
        indices, = np.where(np.logical_and(times >= t0, times <= tf))
        history_indices, = np.where(np.logical_and(
            history_times >= t0, history_times <= tf))
        if (t0 not in times[indices]) and (t0 not in history_times[history_indices]):
            x0 = self.state_at(variable, t0, ensemble_member)
        else:
            t0 = x0 = ca.MX()
        if (tf not in times[indices]) and (tf not in history_times[history_indices]):
            xf = self.state_at(variable, tf, ensemble_member)
        else:
            tf = xf = ca.MX()
        t = ca.vertcat(t0, history_times[history_indices], times[indices], tf)
        x = ca.vertcat(x0, history[history_indices],
                       state[indices[0]:indices[-1] + 1], xf)

        # Integrate knots using trapezoid rule
        x_avg = 0.5 * (x[:x.size1() - 1] + x[1:])
        dt = t[1:] - t[:x.size1() - 1]
        return ca.sum1(x_avg * dt)

    def der(self, variable):
        # Look up the derivative variable for the given non-derivative variable
        canonical, sign = self.alias_relation.canonical_signed(variable)
        try:
            i = self.__differentiated_states_map[canonical]
            return sign * self.dae_variables['derivatives'][i]
        except KeyError:
            try:
                i = self.__algebraic_states_map[canonical]
            except KeyError:
                i = len(self.algebraic_states) + self.__controls_map[canonical]
            return sign * self.__algebraic_and_control_derivatives[i]

    def der_at(self, variable, t, ensemble_member=0):
        # Special case t being t0 for differentiated states
        if t == self.initial_time:
            # We have a special symbol for t0 derivatives
            X = self.solver_input
            control_size = self.__control_size
            ensemble_member_size = int(self.__state_size / self.ensemble_size)

            canonical, sign = self.alias_relation.canonical_signed(variable)
            try:
                i = self.__differentiated_states_map[canonical]
            except KeyError:
                # Fall through, in case 'variable' is not a differentiated state.
                pass
            else:
                nominal = self.variable_nominal(canonical)
                dt = self.__initial_dt[canonical]
                return nominal / dt * sign * X[
                    control_size +
                    (ensemble_member + 1) * ensemble_member_size -
                    len(self.dae_variables['derivatives']) + i]

        # Time stamps for this variable
        times = self.times(variable)

        if t <= self.initial_time:
            # Derivative requested for t0 or earlier.  We need the history.
            history = self.history(ensemble_member)
            try:
                htimes = history[variable].times[:-1]
                history_and_times = np.hstack((htimes, times))
            except KeyError:
                history_and_times = times
        else:
            history_and_times = times

        # Special case t being the initial available point.  In this case, we have
        # no derivative information available.
        if t == history_and_times[0]:
            return 0.0

        # Handle t being an interior point, or t0 for a non-differentiated
        # state
        for i in range(len(history_and_times)):
            # Use finite differences when between collocation points, and
            # backward finite differences when on one.
            if t > history_and_times[i] and t <= history_and_times[i + 1]:
                dx = (self.state_at(variable, history_and_times[i + 1], ensemble_member=ensemble_member) -
                      self.state_at(variable, history_and_times[i], ensemble_member=ensemble_member))
                dt = history_and_times[i + 1] - history_and_times[i]
                return dx / dt

        # t does not belong to any collocation point interval
        raise IndexError

    def map_path_expression(self, expr, ensemble_member):

        f = ca.Function('f', self.__func_orig_inputs, [expr]).expand()
        initial_values = f(*self.__func_initial_inputs[ensemble_member])

        # Replace the original MX symbols with those that were mapped
        [expr_impl] = f.call(self.__func_inputs_implicit)
        f_impl = ca.Function('f_implicit', list(self.__func_accumulated_inputs), [expr_impl]).expand()

        # Map
        fmap = f_impl.map(len(self.times()) - 1)
        values = fmap(*self.__func_mapped_inputs[ensemble_member])

        all_values = ca.horzcat(initial_values, values)

        return ca.transpose(all_values)
