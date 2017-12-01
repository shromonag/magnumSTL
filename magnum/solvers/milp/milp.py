# TODO: add tests where variables are preapplied to constraints
# TODO: add tests for feasible and infeasible constraints
# TODO: Compute eps and M based on x and A, B, dt
# TODO: Add constraint that x < M

from collections import defaultdict
from itertools import chain, product
import operator as op
from functools import partial

import pulp as lp
import funcy as fn
import stl
import traces
from lenses import bind
from funcy import cat, compose

from magnum.game import Game, Specs, Vars
from magnum.constraint_kinds import Kind as K
from magnum.utils import Result
from magnum.solvers.milp import robustness_encoding as rob_encode
from magnum.solvers.milp import boolean_encoding as bool_encode


DEFAULT_NAME = 'controller_synth'


class keydefaultdict(defaultdict):
    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError( key )
        else:
            ret = self[key] = self.default_factory(key)
            return ret


def add_constr(model, constr, kind: K, i: int):
    name = "{}{}".format(kind.name, i)
    model.addConstraint(constr, name=name)


def counter_example_store(times, ce, i):
    def relabel(x):
        return x if i == 0 else f"{x}#{i}"

    return {(relabel(name), t): (trace[t],)
            for (name, trace), t in product(ce.items(), times)}


def encode_game(g, store, ce=None):
    # obj, *non_obj = relabel_specs(g, counter_examples)
    obj, *non_obj = g.specs

    obj = stl.utils.discretize(obj, dt=g.model.dt, distribute=True)
    non_obj = {stl.utils.discretize(phi, dt=g.model.dt, distribute=True)
               for phi in non_obj if phi != stl.TOP}

    # Constraints
    robustness = rob_encode.encode(obj, store, 0)
    # TODO
    dynamics = rob_encode.encode_dynamics(g, store)
    other = cat(bool_encode.encode(psi, store, 0) for psi in non_obj)
    return fn.chain(robustness, dynamics, other), obj


def create_scenario(g, i):
    def relabel(x):
        return x if i == 0 or x in g.model.vars.input else f"{x}#{i}"

    def relabel_phi(phi):
        return stl.ast.lineq_lens(phi).Each().terms.Each().id.modify(relabel)

    # TODO: Fix once python-lenses fixes NamedTuple bug
    g = bind(g).specs.set(Specs(*map(relabel_phi, g.specs)))
    g = bind(g).model.vars.set(Vars(*map(lambda x: tuple(map(relabel, x)), g.model.vars)))
    return g


def game_to_milp(g: Game, robust=True, counter_examples=None):
    # TODO: implement counter_example encoding
    if counter_examples is None:
        counter_examples = [{}]

    model = lp.LpProblem(DEFAULT_NAME, lp.LpMaximize)
    store = keydefaultdict(rob_encode.z)
    # Add counter examples to store
    for i, ce in enumerate(counter_examples):
        store.update(counter_example_store(g.times, ce, i))

    # Encode each scenario.
    scenarios = [create_scenario(g, i) for i, ce in enumerate(counter_examples)]
    constraints, objs = zip(*(encode_game(g2, store) for g2 in scenarios))

    # Objective is to maximize the minimum robustness of the scenarios.
    obj = stl.andf(*objs)
    constraints = chain(rob_encode.encode(obj, store, 0), fn.cat(constraints))

    for i, (constr, kind) in enumerate(constraints):
        if constr is True:
            continue
        add_constr(model, constr, kind, i)

    # TODO: support alternative objective functions
    J = store[obj][0] if isinstance(store[obj], tuple) else store[obj]
    model.setObjective(J)
    return model, store


# Encoding the dynamics

def extract_ts(name, model, g, store):
    dt = g.model.dt
    model = {x: x.value() for x in model.variables()}
    ts = traces.TimeSeries(((dt*t, model[store[name, t][0]])
                             for t in g.times if store[name, t][0] in model)
                           , domain=(0, g.model.H))
    ts.compact()
    return ts


def encode_and_run(g: Game, robust=True, counter_examples=None):
    model, store = game_to_milp(g, robust, counter_examples)
    status = lp.LpStatus[model.solve(lp.solvers.COIN())]

    if status in ('Infeasible', 'Unbounded'):
        return Result(False, None, None)

    elif status == "Optimal":
        cost = model.objective.value()
        sol = {v: extract_ts(v, model, g, store) for v in fn.cat(g.model.vars)}
        return Result(cost > 0, cost, sol)
    else:
        raise NotImplementedError((model, status))

