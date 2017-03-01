"""
TODO: Game to pdf
TODO: Create script to automatically generate game spec
TODO: Include meta information in Game
- Annotate stl with priority
- Annotate stl with name
- Annotate stl with changeability
TODO: add test to make sure phi is hashable after transformation
TODO: create map from SL expr to matching Temporal Logic term after conversion
"""

from itertools import product, chain, starmap, repeat
from functools import partial
from collections import namedtuple, defaultdict
from math import ceil
import operator as op
import pathlib

import yaml
import funcy as fn
from funcy import pluck, group_by, drop, walk_values, compose
import sympy as sym
from lenses import lens

import stl
from stl import STL

import magnum.simplify_mtl

Specs = namedtuple("Specs", "sys env init dyn learned")
Game = namedtuple("Game", "spec model meta")
Model = namedtuple("Model", "dt N vars bounds t")
Vars = namedtuple("Vars", "state input env")
Meta = namedtuple("Meta", "pri names")  # TODO populate


def game_to_stl(g: Game, *, with_init=True, invert_game=False) -> STL:
    phi = g.spec.sys | ~g.spec.env
    if invert_game:
        phi = ~phi
    phi = phi & g.spec.dyn & g.spec.learned
    return phi if with_init else phi & g.spec.init


def discretize_game(g: Game) -> Game:
    specs = Specs(*(discretize_stl(spec, m=g.model) for spec in g.spec))
    return lens(g).spec.set(specs)


def mpc_games(g: Game, endless=False) -> [Game]:
    yield g
    spec_lens = lens(g).spec
    H2 = sym.Dummy("H_2")

    def make_mpc_template(phi):
        return stl.utils.param_lens(stl.G(stl.Interval(0, H2), phi))

    def set_horizon(phi_lens, h2):
        return stl.utils.set_params(phi_lens, {H2: h2})

    templates = Specs(*map(make_mpc_template, g.spec))

    for n in range(1, g.model.N):
        spec = Specs(*(set_horizon(pl, n * g.model.dt) for pl in templates))
        g = lens(spec_lens.set(spec)).model.t.set(n)
        yield g

    while endless:
        yield g


def discrete_mpc_games(g: Game, endless=False) -> [Game]:
    for g in map(discretize_game, mpc_games(g)):
        yield g

    while endless:
        yield g


def negative_time_filter(lineq):
    times = lens(lineq).terms.each_().time.get_all()
    return None if any(t < 0 for t in times) else lineq


filter_none = lambda x: tuple(y for y in x if y is not None)


def discretize_stl(phi: STL, m: Model) -> "LRA":
    # Erase Modal Ops
    psi = stl_to_lra(phi, discretize=partial(discretize, m=m))
    # Set time
    focus = stl.lineq_lens(psi, bind=False)
    psi = set_time(t=0, dt=m.dt, tl=focus.bind(psi).terms.each_())

    # Type cast time to int (and forget about sympy stuff)
    psi = focus.bind(psi).terms.each_().time.modify(int)
    psi = focus.bind(psi).terms.each_().coeff.modify(float)

    # Drop terms from time < 0
    psi = focus.bind(psi).modify(negative_time_filter)
    return stl.and_or_lens(psi).args.modify(filter_none)


def step(t: float, dt: float) -> int:
    return int(t / dt)


def discretize(interval: stl.Interval, m: Model):
    f = lambda x: min(step(x, dt=m.dt), m.N)
    t_0, t_f = interval
    return range(f(t_0), f(t_f) + 1)


def stl_to_lra(phi: STL, discretize) -> "LRA":
    """Returns STL formula with temporal operators erased"""
    return _stl_to_lra([phi], curr_len=lens()[0], discretize=discretize)[0]


def _stl_to_lra(phi, *, curr_len, discretize):
    """Returns STL formula with temporal operators erased"""
    # Warning: _heavily_ uses the lenses library
    # TODO: support Until
    psi = curr_len.get(state=phi)

    # Base Case
    if isinstance(psi, stl.LinEq):
        return phi

    # Erase Time
    if isinstance(psi, stl.ModalOp):
        binop = stl.andf if isinstance(psi, stl.G) else stl.orf

        # Discrete time
        times = discretize(psi.interval)

        # Compute terms lens
        terms = stl.terms_lens(psi.arg)
        psi = binop(*(terms.time + i for i in times))
        phi = curr_len.set(psi, state=phi)

    # Recurse and update Phi
    if isinstance(psi, stl.NaryOpSTL):
        child_lens = (curr_len.args[i] for i in range(len(psi.children())))
        for l in child_lens:
            phi = _stl_to_lra(phi, curr_len=l, discretize=discretize)

    elif isinstance(psi, stl.Neg):
        phi = _stl_to_lra(phi, curr_len=curr_len.arg, discretize=discretize)
    return phi


def set_time(*, t, dt=stl.dt_sym, tl=None, phi=None):
    if tl is None:
        tl = stl.terms_lens(phi)
    focus = tl.tuple_(lens().time, lens().coeff).each_()

    def _set_time(x):
        if hasattr(x, "subs"):
            return x.subs({stl.t_sym: t, stl.dt_sym: dt})
        return x

    return focus.modify(_set_time)


def from_yaml(path) -> Game:
    if isinstance(path, (str, pathlib.Path)):
        with pathlib.Path(path).open("r") as f:
            g = defaultdict(list, yaml.load(f))
    else:
        g = defaultdict(list, yaml.load(f))

    # Parse Specs and Meta
    spec_types = ["sys", "env", "init", "dyn"]
    spec_map = {k: [] for k in spec_types}
    pri_map = {}
    name_map = {}
    dt = float(g['model']['dt'])
    steps = int(ceil(int(g['model']['time_horizon']) / dt))

    for kind in spec_types:
        for spec in g[kind]:
            p = stl.parse(spec['stl'], H=steps)
            name_map[p] = spec.get('name')
            pri_map[p] = spec.get('pri')
            spec_map[kind].append(p)
    spec_map = fn.walk_values(lambda x: stl.andf(*x), spec_map)
    spec_map['learned'] = stl.TOP
    spec = Specs(**spec_map)
    meta = Meta(pri_map, name_map)

    # Parse Model
    stl_var_map = fn.merge({
        'input': [],
        'state': [],
        'env': []
    }, g['model']['vars'])
    stl_var_map['input'] = list(map(sym.Symbol, stl_var_map['input']))
    stl_var_map['state'] = list(map(sym.Symbol, stl_var_map['state']))
    stl_var_map['env'] = list(map(sym.Symbol, stl_var_map['env']))

    bounds = {k: v.split(",") for k, v in g["model"]["bounds"].items()}
    bounds = {
        k: (float(v[0][1:]), float(v[1][:-1]))
        for k, v in bounds.items()
    }
    model = Model(dt=dt, N=steps, vars=Vars(**stl_var_map), bounds=bounds, t=0)

    return Game(spec=spec, model=model, meta=meta)