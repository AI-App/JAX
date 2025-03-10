# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""JAX primitives related to sparse operations.

This is experimental work to explore sparse support in JAX.

The primitives defined here are deliberately low-level: each primitive implements
a common sparse operation (sparse to dense, dense to sparse, sparse matrix/vector
product, sparse matrix/matrix product) for two common sparse representations
(CSR and COO).

These routines have reference implementations defined via XLA scatter/gather
operations that will work on any backend, although they are not particularly
performant. On GPU runtimes built against CUDA 11.0 or newer, each operation is
computed efficiently via cusparse.

Further down are some examples of potential high-level wrappers for sparse objects.
(API should be considered unstable and subject to change).
"""
from functools import partial

import numpy as np

import jax
from jax import core
from jax import tree_util
from jax.experimental.sparse.bcoo import BCOO
from jax.experimental.sparse.coo import COO
from jax.experimental.sparse.csr import CSR, CSC
from jax.experimental.sparse.util import _coo_extract
from jax.interpreters import ad
from jax.interpreters import batching
from jax.interpreters import xla
from jax._src import dtypes
import jax.numpy as jnp


#----------------------------------------------------------------------
# todense – function to convert sparse matrices to dense while letting
#           dense matrices pass through.
todense_p = core.Primitive('todense')
todense_p.multiple_results = False

def todense(arr):
  """Convert input to a dense matrix. If input is already dense, pass through."""
  bufs, tree = tree_util.tree_flatten(arr)
  return todense_p.bind(*bufs, tree=tree)

@todense_p.def_impl
def _todense_impl(*bufs, tree):
  arr = tree_util.tree_unflatten(tree, bufs)
  if isinstance(arr, (jnp.ndarray, np.ndarray)):
    return arr
  return arr.todense()

@todense_p.def_abstract_eval
def _todense_abstract_eval(*bufs, tree):
  arr = tree_util.tree_unflatten(tree, bufs)
  if isinstance(arr, core.ShapedArray):
    return arr
  return core.ShapedArray(arr.shape, arr.dtype, weak_type=dtypes.is_weakly_typed(arr.data))

def _todense_jvp(primals, tangents, *, tree):
  assert not isinstance(tangents[0], ad.Zero)
  assert all(isinstance(t, ad.Zero) for t in tangents[1:])
  primals_out = todense_p.bind(*primals, tree=tree)
  tangents_out = todense_p.bind(tangents[0], *primals[1:], tree=tree)
  return primals_out, tangents_out

def _todense_transpose(ct, *bufs, tree):
  assert ad.is_undefined_primal(bufs[0])
  assert not any(ad.is_undefined_primal(buf) for buf in bufs[1:])

  standin = object()
  obj = tree_util.tree_unflatten(tree, [standin] * len(bufs))
  from jax.experimental.sparse import BCOO, bcoo_extract
  if obj is standin:
    return (ct,)
  elif isinstance(obj, BCOO):
    _, indices = bufs
    return bcoo_extract(indices, ct), indices
  elif isinstance(obj, COO):
    _, row, col = bufs
    return _coo_extract(row, col, ct), row, col
  else:
    raise NotImplementedError(f"todense_transpose for {type(obj)}")

def _todense_batching_rule(batched_args, batch_dims, *, tree):
  return jax.vmap(partial(_todense_impl, tree=tree), batch_dims)(*batched_args), 0

ad.primitive_jvps[todense_p] = _todense_jvp
ad.primitive_transposes[todense_p] = _todense_transpose
batching.primitive_batchers[todense_p] = _todense_batching_rule
xla.register_translation(todense_p, xla.lower_fun(
    _todense_impl, multiple_results=False, new_style=True))


def empty(shape, dtype=None, index_dtype='int32', sparse_format='bcoo', **kwds):
  """Create an empty sparse array.

  Args:
    shape: sequence of integers giving the array shape.
    dtype: (optional) dtype of the array.
    index_dtype: (optional) dtype of the index arrays.
    format: string specifying the matrix format (e.g. ['bcoo']).
    **kwds: additional keywords passed to the format-specific _empty constructor.
  Returns:
    mat: empty sparse matrix.
  """
  formats = {'bcoo': BCOO, 'coo': COO, 'csr': CSR, 'csc': CSC}
  if sparse_format not in formats:
    raise ValueError(f"sparse_format={sparse_format!r} not recognized; "
                     f"must be one of {list(formats.keys())}")
  cls = formats[sparse_format]
  return cls._empty(shape, dtype=dtype, index_dtype=index_dtype, **kwds)
