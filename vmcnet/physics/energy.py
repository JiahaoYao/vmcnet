"""Local energy calculations."""
from typing import Callable, Tuple, TypeVar

import jax
import jax.numpy as jnp

import vmcnet.utils as utils

P = TypeVar("P")  # represents a pytree or pytree-like object containing model params


def laplacian_psi_over_psi(
    grad_log_psi: Callable[[P, jnp.ndarray], jnp.ndarray], params: P, x: jnp.ndarray
) -> jnp.ndarray:
    """Compute (nabla^2 psi) / psi at x given a function which evaluates psi'(x)/psi.

    The computation is done by computing (forward-mode) derivatives of the gradient to
    get the columns of the Hessian, and accumulating the (i, i)th entries (but this
    implementation is significantly more memory efficient than directly computing the
    Hessian).

    This function uses the identity

        (nabla^2 psi) / psi = (nabla^2 log|psi|) + (nabla log|psi|)^2

    to avoid leaving the log domain during the computation.

    This function should be vmapped in order to be applied to batches of inputs, as it
    completely flattens x in order to take second derivatives w.r.t. each component.

    This is approach is extremely similar to the one in the FermiNet repo
    (in the jax branch, as of this writing -- see
    https://github.com/deepmind/ferminet/blob/aade61b3d30883b3238d6b50c85404d0e8176155/ferminet/hamiltonian.py).

    The main difference is that we are being explicit about the flattening of x within
    the Laplacian calculation, so that it does not have to be handled outside of this
    function (psi is free to take x shapes which are not flat).

    Args:
        grad_log_psi (Callable): function which evaluates the derivative of log|psi(x)|,
            i.e. (nabla psi)(x) / psi(x), with respect to x. Has the signature
            (params, x) -> (nabla psi)(x) / psi(x), so the derivative should be over the
            second arg, x, and the output shape should be the same as x
        params (pytree): model parameters, passed as the first arg of grad_log_psi
        x (jnp.ndarray): second input to grad_log_psi

    Returns:
        jnp.ndarray: "local" laplacian calculation, i.e. (nabla^2 psi) / psi
    """
    x_shape = x.shape
    flat_x = jnp.reshape(x, (-1,))
    n = flat_x.shape[0]
    identity_mat = jnp.eye(n)

    def flattened_grad_log_psi_of_flat_x(flat_x_in):
        """Flattened input to flattened output version of grad_log_psi."""
        grad_log_psi_out = grad_log_psi(params, jnp.reshape(flat_x_in, x_shape))
        return jnp.reshape(grad_log_psi_out, (-1,))

    def step_fn(carry, unused):
        del unused
        i = carry[0]
        primals, tangents = jax.jvp(
            flattened_grad_log_psi_of_flat_x, (flat_x,), (identity_mat[i],)
        )
        return (i + 1, carry[1] + jnp.square(primals[i]) + tangents[i]), None

    out, _ = jax.lax.scan(step_fn, (0, 0.0), xs=None, length=n)
    return out[1]


def create_value_and_grad_energy_fn(
    log_psi_apply: Callable[[P, jnp.ndarray], jnp.ndarray],
    local_energy_fn: Callable[[P, jnp.ndarray], jnp.ndarray],
    nchains: int,
) -> Callable[
    [P, jnp.ndarray], Tuple[Tuple[jnp.float32, Tuple[jnp.float32, jnp.ndarray]], P]
]:
    """Create a function which computes unbiased energy gradients.

    Due to the Hermiticity of the Hamiltonian, we can get an unbiased lower variance
    estimate of the gradient of the expected energy than the naive gradient of the
    mean of sampled local energies. Specifically, the gradient of the expected energy
    expect[E_L] takes the form

        2 * expect[(E_L - expect[E_L]) * (grad_psi / psi)(x)],

    where E_L is the local energy and expect[] denotes the expectation with respect to
    the distribution |psi|^2.

    Args:
        log_psi_apply (Callable): computes log|psi(x)|, where the signature of this
            function is (params, x) -> log|psi(x)|
        local_energy_fn (Callable): computes local energies Hpsi / psi. Has signature
            (params, x) -> (Hpsi / psi)(x)
        nchains (int): total number of chains across all devices, used to compute a
            sample variance estimate of the local energy
        optimizer_apply (Callable): applies an update to the parameters. Has signature
            (grad_energy, params, optimizer_state) -> (new_params, new_optimizer_state).

    Returns:
        Callable: function which computes the energy value and gradient. Has signature
            (params, x)
            -> ((expected_energy, auxilliary_energy_data), grad_energy),
        where auxilliary_energy_data is the tuple (expected_variance, local_energies)
    """

    @jax.custom_jvp
    def compute_energy_data(params, positions):
        local_energies = local_energy_fn(params, positions)

        # TODO(Jeffmin) might be worth investigating the numerical stability of the XLA
        # compiled version of these two computations, since the quality of the gradients
        # is fairly crucial to the success of the algorithm
        energy = utils.distribute.pmean_if_pmap(jnp.mean(local_energies))
        variance = (
            utils.distribute.pmean_if_pmap(
                jnp.mean(jnp.square(local_energies - energy))
            )
            * nchains
            / (nchains - 1)
        )  # adjust by n / (n - 1) to get an unbiased estimator
        aux_data = (variance, local_energies)
        return energy, aux_data

    @compute_energy_data.defjvp
    def compute_energy_data_jvp(primals, tangents):
        params, positions = primals
        energy, aux_data = compute_energy_data(params, positions)
        _, local_energies = aux_data

        _, psi_tangents = jax.jvp(log_psi_apply, primals, tangents)
        primals_out = (energy, aux_data)
        tangents_out = (2.0 * jnp.dot(psi_tangents, local_energies - energy), aux_data)
        return primals_out, tangents_out

    energy_data_val_and_grad = jax.value_and_grad(
        compute_energy_data, argnums=0, has_aux=True
    )
    return energy_data_val_and_grad
