# Copyright 2018-2019 QuantumBlack Visual Analytics Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND
# NONINFRINGEMENT. IN NO EVENT WILL THE LICENSOR OR OTHER CONTRIBUTORS
# BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF, OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# The QuantumBlack Visual Analytics Limited (“QuantumBlack”) name and logo
# (either separately or in combination, “QuantumBlack Trademarks”) are
# trademarks of QuantumBlack. The License does not grant you any right or
# license to the QuantumBlack Trademarks. You may not use the QuantumBlack
# Trademarks or any confusingly similar mark as a trademark for your product,
#     or use the QuantumBlack Trademarks in any other manner that might cause
# confusion in the marketplace, including but not limited to in advertising,
# on websites, or on software.
#
# See the License for the specific language governing permissions and
# limitations under the License.
"""A ``Pipeline`` is a collection of ``Node`` objects which can be executed as
a Directed Acyclic Graph, sequentially or in parallel. The ``Pipeline`` class
offers quick access to input dependencies,
produced outputs and execution order.
"""
import copy
import json
from collections import Counter, defaultdict
from itertools import chain
from typing import Callable, Dict, Iterable, List, Set, Union

from toposort import CircularDependencyError as ToposortCircleError
from toposort import toposort

import kedro
from kedro.pipeline.node import Node


class OutputNotUniqueError(Exception):
    """Raised when two or more nodes that are part of the same pipeline
    produce outputs with the same name.
    """

    pass


class Pipeline:
    """A ``Pipeline`` defined as a collection of ``Node`` objects. This class
    treats nodes as part of a graph representation and provides inputs,
    outputs and execution order.
    """

    def __init__(self, nodes: Iterable[Union[Node, "Pipeline"]], name: str = None):
        """Initialise ``Pipeline`` with a list of ``Node`` instances.

        Args:
            nodes: The list of nodes the ``Pipeline`` will be made of. If you
                provide pipelines among the list of nodes, those pipelines will
                be expanded and all their nodes will become part of this
                new pipeline.
            name: The name of the pipeline. If specified, this name
                will be used to tag all of the nodes in the pipeline.
        Raises:
            ValueError:
                When an empty list of nodes is provided, or when not all
                nodes have unique names.
            CircularDependencyError:
                When visiting all the nodes is not
                possible due to the existence of a circular dependency.
            OutputNotUniqueError:
                When multiple ``Node`` instances produce the same output.
        Example:
        ::

            >>> from kedro.pipeline import Pipeline
            >>> from kedro.pipeline import node
            >>>
            >>> # In the following scenario first_ds and second_ds
            >>> # are data sets provided by io. Pipeline will pass these
            >>> # data sets to first_node function and provides the result
            >>> # to the second_node as input.
            >>>
            >>> def first_node(first_ds, second_ds):
            >>>     return dict(third_ds=first_ds+second_ds)
            >>>
            >>> def second_node(third_ds):
            >>>     return third_ds
            >>>
            >>> pipeline = Pipeline([
            >>>     node(first_node, ['first_ds', 'second_ds'], ['third_ds']),
            >>>     node(second_node, dict(third_ds='third_ds'), 'fourth_ds')])
            >>>
            >>> pipeline.describe()
            >>>

        """
        _validate_no_node_list(nodes)
        nodes = list(
            chain.from_iterable(
                [[n] if isinstance(n, Node) else n.nodes for n in nodes]
            )
        )
        _validate_duplicate_nodes(nodes)
        _validate_namespaced_inputs_outputs(nodes)

        if name:
            nodes = [n.tag([name]) for n in nodes]
        self._name = name
        self._nodes_by_name = {node.name: node for node in nodes}
        _validate_unique_outputs(nodes)

        self._nodes_by_input = defaultdict(set)  # input: {nodes with input}
        for node in nodes:
            for input_ in node.input_namespaces:
                self._nodes_by_input[input_].add(node)

        self._nodes_by_output = {}  # output: node
        for node in nodes:
            for output in node.output_namespaces:
                self._nodes_by_output[output] = node

        self._nodes = nodes
        self._topo_sorted_nodes = _topologically_sorted(self.node_dependencies)

    def __repr__(self):  # pragma: no cover
        reprs = [repr(node) for node in self.nodes]
        return "{}([\n{}\n])".format(self.__class__.name, ",\n".join(reprs))

    def __add__(self, other):
        if not isinstance(other, Pipeline):
            return NotImplemented
        return Pipeline(set(self.nodes + other.nodes))

    def all_inputs(self) -> Set[str]:
        """All inputs for all nodes in the pipeline.

        Returns:
            All node input names as a Set.

        """
        return set.union(set(), *[node.inputs for node in self.nodes])

    def all_outputs(self) -> Set[str]:
        """All outputs of all nodes in the pipeline.

        Returns:
            All node outputs.

        """
        return set.union(set(), *[node.outputs for node in self.nodes])

    def _remove_intermediates(self, datasets: Set[str]) -> Set[str]:
        intermediate = {Node.get_namespace(i) for i in self.all_inputs()} & {
            Node.get_namespace(o) for o in self.all_outputs()
        }
        return {d for d in datasets if Node.get_namespace(d) not in intermediate}

    def inputs(self) -> Set[str]:
        """The names of free inputs that must be provided at runtime so that
        the pipeline is runnable. Does not include intermediate inputs which
        are produced and consumed by the inner pipeline nodes. Resolves
        namespaces where necessary.

        Returns:
            The set of free input names needed by the pipeline.

        """
        return self._remove_intermediates(self.all_inputs())

    def outputs(self) -> Set[str]:
        """The names of outputs produced when the whole pipeline is run.
        Does not include intermediate outputs that are consumed by
        other pipeline nodes. Resolves namespaces where necessary.

        Returns:
            The set of final pipeline outputs.

        """
        return self._remove_intermediates(self.all_outputs())

    def data_sets(self) -> Set[str]:
        """The names of all data sets used by the ``Pipeline``,
        including inputs and outputs.

        Returns:
            The set of all pipeline data sets.

        """
        return self.all_outputs() | self.all_inputs()

    def describe(self, names_only: bool = True) -> str:
        """Obtain the order of execution and expected free input variables in
        a loggable pre-formatted string. The order of nodes matches the order
        of execution given by the topological sort.

        Args:
            names_only: The flag to describe names_only pipeline with just
                node names.

        Example:
        ::

            >>> pipeline = Pipeline([ ... ])
            >>>
            >>> logger = logging.getLogger(__name__)
            >>>
            >>> logger.info(pipeline.describe())

        After invocation the following will be printed as an info level log
        statement:
        ::

            #### Pipeline execution order ####
            Inputs: C, D

            func1([C]) -> [A]
            func2([D]) -> [B]
            func3([A, D]) -> [E]

            Outputs: B, E
            ##################################

        Returns:
            The pipeline description as a formatted string.

        """

        def set_to_string(set_of_strings):
            """Convert set to a string but return 'None' in case of an empty
            set.
            """
            return ", ".join(sorted(set_of_strings)) if set_of_strings else "None"

        nodes_as_string = "\n".join(
            node.name if names_only else str(node) for node in self.nodes
        )

        str_representation = (
            "#### Pipeline execution order ####\n"
            "Name: {0}\n"
            "Inputs: {1}\n\n"
            "{2}\n\n"
            "Outputs: {3}\n"
            "##################################"
        )

        return str_representation.format(
            self._name,
            set_to_string(self.inputs()),
            nodes_as_string,
            set_to_string(self.outputs()),
        )

    @property
    def name(self) -> str:
        """Get the pipeline name.

        Returns:
            The name of the pipeline as provided in the constructor.

        """
        return self._name

    @property
    def node_dependencies(self) -> Dict[Node, Set[Node]]:
        """All dependencies of nodes where the first Node has a direct dependency on
        the second Node.

        Returns:
            Dictionary where keys are nodes and values are sets made up of
            their parent nodes. Independent nodes have this as empty sets.
        """
        dependencies = {node: set() for node in self._nodes}
        for parent in self._nodes:
            for output in parent.output_namespaces:
                for child in self._nodes_by_input[output]:
                    dependencies[child].add(parent)

        return dependencies

    @property
    def nodes(self) -> List[Node]:
        """Return a list of the pipeline nodes in topological order, i.e. if
        node A needs to be run before node B, it will appear earlier in the
        list.

        Returns:
            The list of all pipeline nodes in topological order.

        """
        return list(chain.from_iterable(self._topo_sorted_nodes))

    @property
    def grouped_nodes(self) -> List[Set[Node]]:
        """Return a list of the pipeline nodes in topologically ordered groups,
        i.e. if node A needs to be run before node B, it will appear in an
        earlier group.

        Returns:
            The pipeline nodes in topologically ordered groups.

        """
        return copy.copy(self._topo_sorted_nodes)

    def only_nodes(self, *node_names: str) -> "Pipeline":
        """Create a new ``Pipeline`` which will contain only the specified
        nodes by name.

        Args:
            node_names: One or more node names. The returned ``Pipeline``
                will only contain these nodes.

        Raises:
            ValueError: When some invalid node name is given.

        Returns:
            A new ``Pipeline``, containing only ``nodes``.

        """
        unregistered_nodes = set(node_names) - set(self._nodes_by_name.keys())
        if unregistered_nodes:
            raise ValueError(
                "Pipeline does not contain nodes named {}.".format(
                    list(unregistered_nodes)
                )
            )

        nodes = [self._nodes_by_name[name] for name in node_names]
        return Pipeline(nodes)

    def only_nodes_with_inputs(self, *inputs: str) -> "Pipeline":
        """Create a new ``Pipeline`` object with the nodes which depend
         directly on the provided inputs.

        Args:
            inputs: A list of inputs which should be used as a starting
                point of the new ``Pipeline``.

        Raises:
            ValueError: Raised when any of the given inputs do not exist in the
                ``Pipeline`` object.

        Returns:
            A new ``Pipeline`` object, containing a subset of the
                nodes of the current one such that only nodes depending
                directly on the provided inputs are being copied.

        """
        starting = set(inputs)

        missing = sorted(starting - self.data_sets())
        if missing:
            raise ValueError(
                "Pipeline does not contain data_sets named {}".format(missing)
            )

        nodes = set(
            chain.from_iterable(self._nodes_by_input[input_] for input_ in starting)
        )

        return Pipeline(nodes)

    def from_inputs(self, *inputs: str) -> "Pipeline":
        """Create a new ``Pipeline`` object with the nodes which depend
         directly or transitively on the provided inputs.

        Args:
            inputs: A list of inputs which should be used as a starting point
                of the new ``Pipeline``

        Raises:
            ValueError: Raised when any of the given inputs do not exist in the
                ``Pipeline`` object.

        Returns:
            A new ``Pipeline`` object, containing a subset of the
                nodes of the current one such that only nodes depending
                directly or transitively on the provided inputs are being
                copied.

        """
        starting = set(inputs)

        missing = sorted(starting - self.data_sets())
        if missing:
            raise ValueError(
                "Pipeline does not contain data_sets named {}".format(missing)
            )

        all_nodes = set()
        while True:
            nodes = set(
                chain.from_iterable(self._nodes_by_input[input_] for input_ in starting)
            )

            outputs = set(chain.from_iterable(node.outputs for node in nodes))

            starting = outputs
            all_nodes |= nodes
            if not nodes:
                break

        return Pipeline(all_nodes)

    def only_nodes_with_outputs(self, *outputs: str) -> "Pipeline":
        """Create a new ``Pipeline`` object with the nodes which are directly
        required to produce the provided outputs.

        Args:
            outputs: A list of outputs which should be the final outputs
                of the new ``Pipeline``.

        Raises:
            ValueError: Raised when any of the given outputs do not exist in
                the ``Pipeline`` object.

        Returns:
            A new ``Pipeline`` object, containing a subset of the nodes of the
            current one such that only nodes which are directly required to
            produce the provided outputs are being copied.
        """
        starting = set(outputs)

        missing = sorted(starting - self.data_sets())
        if missing:
            raise ValueError(
                "Pipeline does not contain data_sets named {}".format(missing)
            )

        nodes = {
            self._nodes_by_output[output]
            for output in starting
            if output in self._nodes_by_output
        }

        return Pipeline(nodes)

    def to_outputs(self, *outputs: str) -> "Pipeline":
        """Create a new ``Pipeline`` object with the nodes which are directly
        or transitively required to produce the provided outputs.

        Args:
            outputs: A list of outputs which should be the final outputs of
                the new ``Pipeline``.

        Raises:
            ValueError: Raised when any of the given outputs do not exist in
                the ``Pipeline`` object.

        Returns:
            A new ``Pipeline`` object, containing a subset of the nodes of the
            current one such that only nodes which are directly or transitively
            required to produce the provided outputs are being copied.

        """
        starting = set(outputs)

        missing = sorted(starting - self.data_sets())
        if missing:
            raise ValueError(
                "Pipeline does not contain data_sets named {}".format(missing)
            )

        all_nodes = set()
        while True:
            nodes = {
                self._nodes_by_output[output]
                for output in starting
                if output in self._nodes_by_output
            }

            inputs = set(chain.from_iterable(node.inputs for node in nodes))

            starting = inputs
            all_nodes |= nodes
            if not nodes:
                break

        return Pipeline(all_nodes)

    def from_nodes(self, *node_names: str) -> "Pipeline":
        """Create a new ``Pipeline`` object with the nodes which depend
        directly or transitively on the provided nodes.

        Args:
            node_names: A list of node_names which should be used as a
                starting point of the new ``Pipeline``.
        Raises:
            ValueError: Raised when any of the given names do not exist in the
                ``Pipeline`` object.
        Returns:
            A new ``Pipeline`` object, containing a subset of the nodes of
                the current one such that only nodes depending directly or
                transitively on the provided nodes are being copied.

        """

        res = self.only_nodes(*node_names)
        res += self.from_inputs(*res.all_outputs())
        return res

    def to_nodes(self, *node_names: str) -> "Pipeline":
        """Create a new ``Pipeline`` object with the nodes required directly
        or transitively by the provided nodes.

        Args:
            node_names: A list of node_names which should be used as a
                starting point of the new ``Pipeline``.
        Raises:
            ValueError: Raised when any of the given names do not exist in the
                ``Pipeline`` object.
        Returns:
            A new ``Pipeline`` object, containing a subset of the nodes of the
                current one such that only nodes required directly or
                transitively by the provided nodes are being copied.

        """

        res = self.only_nodes(*node_names)
        res += self.to_outputs(*res.all_inputs())
        return res

    def only_nodes_with_tags(self, *tags: str) -> "Pipeline":
        """Create a new ``Pipeline`` object with the nodes which contain *any*
        of the provided tags. The resulting ``Pipeline`` is empty if no tags
        are provided.

        Args:
            tags: A list of node tags which should be used to lookup
                the nodes of the new ``Pipeline``.
        Returns:
            Pipeline: A new ``Pipeline`` object, containing a subset of the
                nodes of the current one such that only nodes containing *any*
                of the tags provided are being copied.
        """
        tags = set(tags)
        nodes = [node for node in self.nodes if tags & node.tags]
        return Pipeline(nodes)

    def decorate(self, *decorators: Callable) -> "Pipeline":
        """Create a new ``Pipeline`` by applying the provided decorators to
        all the nodes in the pipeline. If no decorators are passed, it will
        return a copy of the current ``Pipeline`` object.

        Args:
            decorators: List of decorators to be applied on
                all node functions in the pipeline. Decorators will be applied
                from right to left.

        Returns:
            A new ``Pipeline`` object with all nodes decorated with the
            provided decorators.

        """
        nodes = [node.decorate(*decorators) for node in self.nodes]
        return Pipeline(nodes)

    def to_json(self):
        """Return a json representation of the pipeline."""
        transformed = [
            {
                "name": n.name,
                "inputs": list(n.input_namespaces),
                "outputs": list(n.output_namespaces),
                "tags": list(n.tags),
            }
            for n in self.nodes
        ]
        pipeline_versioned = {
            "kedro_version": kedro.__version__,
            "pipeline": transformed,
        }

        return json.dumps(pipeline_versioned)


def _validate_no_node_list(nodes: Iterable[Node]):
    if nodes is None:
        raise ValueError(
            "`nodes` argument of `Pipeline` is None. "
            "Must be a list of nodes instead."
        )


def _validate_duplicate_nodes(nodes: List[Node]):
    names = [node.name for node in nodes]
    duplicate = [key for key, value in Counter(names).items() if value > 1]
    if duplicate:
        raise ValueError(
            "Pipeline nodes must have unique names. The "
            "following node names appear more than once: {}\n"
            "You can name your nodes using the last argument "
            "of `node()`.".format(duplicate)
        )


def _validate_unique_outputs(nodes: List[Node]) -> None:
    outputs_list = list(chain.from_iterable(node.output_namespaces for node in nodes))
    counter_list = Counter(outputs_list)
    counter_set = Counter(set(outputs_list))
    diff = counter_list - counter_set
    if diff:
        raise OutputNotUniqueError(
            "Output(s) {} are returned by "
            "more than one nodes. Node "
            "outputs must be unique.".format(sorted(diff.keys()))
        )


def _validate_namespaced_inputs_outputs(nodes: List[Node]) -> None:
    """Users should not be allowed to refer to a dataset without
    the separator if it is referenced later on in the pipeline.
    """
    all_inputs_outputs = set(
        chain(
            chain.from_iterable(node.inputs for node in nodes),
            chain.from_iterable(node.outputs for node in nodes),
        )
    )

    invalid = set()
    for dataset_name in all_inputs_outputs:
        namespace = Node.get_namespace(dataset_name)
        if namespace != dataset_name and namespace in all_inputs_outputs:
            invalid.add(namespace)

    if invalid:
        raise ValueError(
            "The following datasets are used with transcoding, but "
            "were referenced without the separator: {}.\n"
            "Please specify a transcoding option or "
            "rename the datasets.".format(", ".join(invalid))
        )


def _topologically_sorted(node_dependencies) -> List[Set[Node]]:
    """Topologically group and sort (order) nodes such that no node depends on
    a node that appears in the same or a later group.

    Raises:
        CircularDependencyError: When it is not possible to topologically order
            provided nodes.

    Returns:
        The list of node sets in order of execution. First set is nodes that should
        be executed first (no dependencies), second set are nodes that should be
        executed on the second step, etc.
    """

    def _circle_error_message(error_data: Dict[str, str]) -> str:
        """Error messages provided by the toposort library will
        refer to indices that are used as an intermediate step.
        This method can be used to replace that message with
        one that refers to the nodes' string representations.
        """
        circular = [str(node) for node in error_data.keys()]
        return "Circular dependencies exist among these items: {}".format(circular)

    try:
        return list(toposort(node_dependencies))
    except ToposortCircleError as error:
        message = _circle_error_message(error.data)
        raise CircularDependencyError(message) from error


class CircularDependencyError(Exception):
    """Raised when it is not possible to provide a topological execution
    order for nodes, due to a circular dependency existing in the node
    definition.
    """

    pass
