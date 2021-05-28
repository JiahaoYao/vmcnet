"""Proposal and acceptance fns for Metropolis-Hastings Markov-Chain Monte Carlo."""
from typing import Callable, Tuple, TypeVar

import flax
import jax
import jax.numpy as jnp

from vmcnet.updates.data import PositionAmplitudeData, update_position_amplitude
import vmcnet.mcmc as mcmc

# represents a pytree or pytree-like object containing MCMC data, e.g. walker positions
# and wave function amplitudes, or other auxilliary MCMC data
D = TypeVar("D")
P = TypeVar("P")  # to represent a pytree or pytree-like object containing model params


def make_metropolis_step(
    proposal_fn: Callable[[P, D, jnp.ndarray], Tuple[D, jnp.ndarray]],
    acceptance_fn: Callable[[P, D, D], jnp.ndarray],
    update_data_fn: Callable[[D, D, jnp.ndarray], D],
) -> Callable[[P, D, jnp.ndarray], Tuple[jnp.float32, D, jnp.ndarray]]:
    """Factory to create a function which takes a single metropolis step.

    Following Metropolis-Hastings Markov Chain Monte Carlo, a transition from one data
    state to another is split into proposal and acceptance. When used in a Metropolis
    routine to approximate a stationary distribution P, the proposal and acceptance
    functions should satisfy detailed balance, i.e.,

        proposal_prob_ij * acceptance_ij * P_i = proposal_prob_ji * acceptance_ji * P_j,

    where proposal_prob_ij is the likelihood of proposing the transition from state i to
    state j, acceptance_ij is the likelihood of accepting a transition from state i
    to state j, and P_i is the probability of being in state i.

    Args:
        proposal_fn (Callable): proposal function which produces new proposed data. Has
            the signature (params, data, key) -> proposed_data, key
        acceptance_fn (Callable): acceptance function which produces a vector of numbers
            used to create a mask for accepting the proposals. Has the signature
            (params, data, proposed_data) -> jnp.ndarray: acceptance probabilities
        update_data_fn (Callable): function used to update the data given the original
            data, the proposed data, and the array mask identifying which proposals to
            accept. Has the signature
            (data, proposed_data, mask) -> new_data

    Returns:
        Callable: function which takes in (data, params, key) and outputs
        (mean acceptance probability, new data, new jax PRNG key split from previous
        one)
    """

    def metrop_step_fn(data, params, key):
        """Take a single metropolis step."""
        key, subkey = jax.random.split(key)
        proposed_data, key = proposal_fn(params, data, key)
        accept_prob = acceptance_fn(params, data, proposed_data)
        move_mask = jax.random.uniform(subkey, shape=accept_prob.shape) < accept_prob
        new_data = update_data_fn(data, proposed_data, move_mask)

        return jnp.mean(accept_prob), new_data, key

    return metrop_step_fn


def gaussian_proposal(
    positions: jnp.ndarray, std_move: jnp.float32, key: jnp.ndarray
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """Simple symmetric gaussian proposal in all positions at once.

    Args:
        positions (jnp.ndarray): original positions
        std_move (jnp.float32): standard deviation of the moves
        key (jnp.ndarray): an array with shape (2,) representing a jax PRNG key

    Returns:
        (jnp.ndarray, jnp.ndarray): (new positions, new key split from previous)
    """
    key, subkey = jax.random.split(key)
    return positions + std_move * jax.random.normal(subkey, shape=positions.shape), key


def metropolis_symmetric_acceptance(
    amplitude: jnp.ndarray, proposed_amplitude: jnp.ndarray, logabs: bool = True
) -> jnp.ndarray:
    """Standard Metropolis acceptance ratio for a symmetric proposal function.

    The general Metropolis-Hastings choice of acceptance ratio for moves from state i to
    state j is given by

        accept_ij = min(1, (P_j * proposal_prob_ji) / (P_i * proposal_prob_ij)).

    When proposal_prob is symmetric (assumed in this function), this simply reduces to
    accept_ij = min(1, P_j / P_i). Some care is taken to avoid numerical overflow and
    division by zero.

    The inputs are wavefunction amplitudes psi or log(|psi|), so the probability P_i
    refers to |psi(i)|^2.

    Args:
        amplitude (jnp.ndarray): one-dimensional array of wavefunction amplitudes for
            the current state, or log wavefunction amplitudes if logabs is True
        proposed_amplitude (jnp.ndarray): one-dimensional array of wavefunction
            amplitudes for the proposed state, or log wavefunction amplitudes if logabs
            is True
        logabs (bool, optional): whether the provided amplitudes represent psi
            (logabs = False) or log|psi| (logabs = True). Defaults to True.

    Returns:
        jnp.ndarray: one-dimensional array of acceptance ratios for the Metropolis
        algorithm
    """
    if not logabs:
        prob_old = jnp.square(amplitude)
        prob_new = jnp.square(proposed_amplitude)
        ratio = prob_new / prob_old
        # safe division by zero
        ratio = jnp.where(
            jnp.logical_or(prob_old < prob_new, prob_old == 0.0),
            jnp.ones_like(ratio),
            ratio,
        )
        return ratio

    log_prob_old = 2.0 * amplitude
    log_prob_new = 2.0 * proposed_amplitude
    # avoid overflow if log_prob_new - log_prob_old is large
    return jnp.where(
        log_prob_new > log_prob_old,
        jnp.ones_like(log_prob_new),
        jnp.exp(log_prob_new - log_prob_old),
    )


def make_position_amplitude_gaussian_proposal(
    model_apply: Callable[[P, jnp.ndarray], jnp.ndarray], std_move: jnp.float32
) -> Callable[
    [P, PositionAmplitudeData, jnp.ndarray], Tuple[PositionAmplitudeData, jnp.ndarray]
]:
    """Factory to make a gaussian proposal on PositionAmplitudeData.

    Args:
        model_apply (Callable): function which evaluates a model. Has signature
            (params, position) -> amplitude
        std_move (jnp.float32): standard deviation of the proposed moves

    Returns:
        Callable: proposal function which can be passed to the main VMC routine. Has
        signature (params, PositionAmplitudeData, key) -> (PositionAmplitudeData, key).
    """

    def proposal_fn(params, data, key):
        proposed_position, key = gaussian_proposal(data.position, std_move, key)
        proposed_amplitude = model_apply(params, proposed_position)
        return PositionAmplitudeData(proposed_position, proposed_amplitude), key

    return proposal_fn


def make_position_amplitude_metropolis_symmetric_acceptance(
    logabs: bool = True,
) -> Callable[[P, PositionAmplitudeData, PositionAmplitudeData], jnp.ndarray]:
    """Factory to make a Metropolis acceptance function on PositionAmplitudeData.

    Args:
        logabs (bool, optional): whether amplitudes provided to `acceptance_fn`
            represent psi (logabs = False) or log|psi| (logabs = True). Defaults to
            True.

    Returns:
        Callable: acceptance function which can be passed to the main VMC routine. Has
        signature (params, PositionAmplitudeData, PositionAmplitudeData) -> accept_ratio
    """

    def acceptance_fn(params, data, proposed_data):
        del params
        return metropolis_symmetric_acceptance(
            data.amplitude, proposed_data.amplitude, logabs=logabs
        )

    return acceptance_fn


def make_position_amplitude_gaussian_metropolis_step(
    std_move: jnp.float32,
    model_apply: Callable[[P, jnp.ndarray], jnp.ndarray],
    logabs: bool = True,
) -> Callable[
    [P, PositionAmplitudeData, jnp.ndarray],
    Tuple[jnp.float32, PositionAmplitudeData, jnp.ndarray],
]:
    """Make a gaussian proposal with Metropolis acceptance for PositionAmplitudeData.

    Args:
        std_move (jnp.float32): standard deviation of the proposed moves
        model_apply (Callable): function which evaluates a model. Has signature
            (params, position) -> amplitude
        logabs (bool, optional): whether the provided amplitudes represent psi
            (logabs = False) or log|psi| (logabs = True). Defaults to True.

    Returns:
        Callable: function which does a metropolis step. Has the signature
            (params, PositionAmplitudeData, key)
            -> (mean acceptance probability, PositionAmplitudeData, new_key)
    """
    proposal_fn = mcmc.metropolis.make_position_amplitude_gaussian_proposal(
        model_apply, std_move
    )
    accept_fn = mcmc.metropolis.make_position_amplitude_metropolis_symmetric_acceptance(
        logabs=logabs
    )
    metrop_step_fn = mcmc.metropolis.make_metropolis_step(
        proposal_fn, accept_fn, update_position_amplitude
    )
    return metrop_step_fn
