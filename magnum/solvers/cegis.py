from itertools import cycle

import stl
import funcy as fn
from lenses import lens

import magnum
from magnum.solvers.milp import encode_and_run as predict
from magnum.utils import project_solution_stl

from enum import Enum, auto

class CegisState(Enum):
    INITIAL_LOOP = auto()
    U_DOMINANT = auto()
    W_DOMINANT = auto()
    W_NOT_DOMINANT = auto()
    U_NOT_DOMINANT = auto()
    MAYBE_REACTIVE = auto() 


TERMINATION_STATES = {
    CegisState.MAYBE_REACTIVE, 
    CegisState.W_DOMINANT,
    CegisState.U_DOMINANT
}

def cegis(g):
    terminate = lambda x: x[1] in TERMINATION_STATES
    response, state = fn.first(filter(termination_states, cegis_loop(g)))
    return state if state == CegisState.U_DOMINANT else None


def update_state(response, is_p1, converged, state):
    """CEGIS State Machine"""
    if not response.feasible:
        if is_p1:
            return CegisState.W_DOMINANT
        else:
            return CegisState.U_DOMINANT
    elif converged:
        if is_p1:
            if state == W_NOT_DOMINANT:
                return CegisState.MAYBE_REACTIVE
            else:
                return CegisState.U_NOT_DOMINANT
        else:
            if state == U_NOT_DOMINANT:
                return CegisState.MAYBE_REACTIVE
            else:
                return CegisState.W_NOT_DOMINANT
    else:
        return state
    

def cegis_loop(g):
    """CEGIS for dominant/robust strategy.
    ∃u∀w . (x(u, w, t), u, w) ⊢ φ
    """
    # Create player for sys and env resp.
    p1 = player(g)
    p2 = player(magnum.game.invert_game(g))

    # Start Co-Routines
    next(p1)
    next(p2)

    # Take turns providing counter examples
    response = set()
    state = CegisState.INITIAL_LOOP
    for p in cycle([p1, p2]):
        # Tell p about previous response and get p's response.
        response, converged = p.send(response)

        # Update State
        state = update_state(response, p == p1, converged, state)
            
        yield response, state


def player(g):
    """Player co-routine.
    Receives counter example and then returns response. 
    """
    converged = False
    learned_lens = lens(g).spec.learned
    inputs, adv_inputs = g.model.vars.input, g.model.vars.env

    counter_example = yield
    # We must be the first player. Give unconstrained response.
    if not counter_example:
        counter_example = yield predict(g), converged
    banned_inputs = set()

    while True:
        # They gave a response w, we cannot use previous solutions.
        sol = counter_example.solution
        prev_input = project_solution_stl(sol, inputs, g.model.t)
        response = project_solution_stl(sol, adv_inputs, g.model.t)
        # Step 1) prev input had counter strategy, so ban it.
        if prev_input is not stl.TOP:
            banned_inputs.add(prev_input)

        # Step 2) respond to w's response.
        learned = response & ~stl.orf(*banned_inputs)
        prediction = predict(learned_lens.set(learned))
            
        # Step 3) Consider old inputs upon failure
        if not prediction.feasible:
            converged = True
            learned = response & stl.orf(*banned_inputs)
            prediction = predict(learned_lens.set(learned))

        # Step 4) Yield response
        counter_example = yield prediction, converged
