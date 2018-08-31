from collections import namedtuple
from itertools import chain
from copy import deepcopy

# TODO: abstract as singleton executor under hades namespace
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
executor = ThreadPoolExecutor(max_workers=4)

from plucky import merge
import dimod


class SampleSet(dimod.Response):

    def __eq__(self, other):
        # TODO: merge into dimod.Response
        return (self.vartype == other.vartype and self.info == other.info
            and self.variable_labels == other.variable_labels
            and self.record == other.record)

    @property
    def first(self):
        """Return the `Sample(sample={...}, energy, num_occurrences)` with
        lowest energy.
        """
        # TODO: merge into dimod.Response
        return next(self.data(sorted_by='energy', name='Sample'))

    @classmethod
    def from_sample(cls, sample, vartype, energy=None, num_occurrences=1):
        """Convenience method for constructing a SampleSet from one raw (dict)
        sample.
        """
        return cls.from_samples(
            samples_like=[sample],
            vectors={'energy': [energy], 'num_occurrences': [num_occurrences]},
            info={}, vartype=vartype)

    @classmethod
    def from_sample_on_bqm(cls, sample, bqm):
        return cls.from_sample(sample, bqm.vartype, bqm.energy(sample))

    @classmethod
    def from_response(cls, response):
        return cls.from_future(response, result_hook=lambda x: x)


_State = namedtuple('State', 'samples ctx debug')

class State(_State):
    """Computation state passed along a branch between connected components.
    The structure is fixed, but fields are mutable. Components can store
    context into `ctx` and debugging/tracing info into `debug`.

    NB: Based on _State namedtuple, with added default values/kwargs.
    """

    def __new__(_cls, samples=None, ctx=None, debug=None):
        """`samples` is SampleSet, `ctx` and `debug` are `dict`."""
        if ctx is None:
            ctx = {}
        if debug is None:
            debug = {}
        return _State.__new__(_cls, samples, ctx, debug)

    def _update(self, src, upt):
        # depth-limited recursive plucky.merge (max-depth=2, level 2+ copied)
        dest = {}
        for key in set(chain(src.keys(), upt.keys())):
            if key in src and key in upt:
                if isinstance(upt[key], dict):
                    dest[key] = deepcopy(src[key])
                    for subkey, subval in upt[key].items():
                        dest[key][subkey] = deepcopy(subval)
                else:
                    dest[key] = deepcopy(upt[key])
            elif key in src:
                dest[key] = deepcopy(src[key])
            elif key in upt:
                dest[key] = deepcopy(upt[key])
        return dest

    def updated(self, **kwargs):
        return State(**self._update(self._asdict(), kwargs))

    def copy(self):
        return deepcopy(self)

    @classmethod
    def from_sample(cls, sample, bqm):
        """Convenience method for constructing State from raw (dict) sample;
        energy is calculated from BQM.
        """
        return cls(SampleSet.from_sample(sample,
                                         vartype=bqm.vartype,
                                         energy=bqm.energy(sample)))


class Present(object):
    """Already resolved Future-like object.

    Very limited in Future-compatibility. We implement only the minimum here.
    """

    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result

    def done(self):
        return True


class Runnable(object):
    """Runnable component can be run for one iteration at a time. Iteration
    might be stopped, but implementing stop support is not required.
    """

    def __init__(self, *args, **kwargs):
        super(Runnable, self).__init__(*args, **kwargs)

    def iterate(self, state):
        """Accept a state and return a new state (blocking)."""
        raise NotImplementedError

    def run(self, state):
        """Accept a state in future and return a new state in future (async)."""
        try:
            state = state.result()
        except AttributeError:
            pass
        return executor.submit(self.iterate, state)

    def stop(self):
        pass

    def __or__(self, other):
        """Composition of runnable components (L-to-R) returns a new runnable Branch."""
        return Branch(components=(self, other))


class Branch(Runnable):
    def __init__(self, components=(), *args, **kwargs):
        """Sequentially executed components.

        `components` is an iterable of `Runnable`s.
        """
        super(Branch, self).__init__(*args, **kwargs)
        self.components = tuple(components)

    def __or__(self, other):
        """Composition of Branch with runnable components (L-to-R) returns a new
        runnable Branch.
        """
        if isinstance(other, Branch):
            return Branch(components=chain(self.components, other.components))
        elif isinstance(other, Runnable):
            return Branch(components=chain(self.components, (other,)))
        else:
            raise TypeError("branch can be composed only with Branch or Runnable")

    def iterate(self, state):
        for component in self.components:
            state = component.iterate(state)
        return state

    def stop(self):
        for component in self.components:
            component.stop()
