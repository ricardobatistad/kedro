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
"""``AbstractRunner`` is the base class for all ``Pipeline`` runner
implementations.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

from kedro.io import AbstractDataSet, DataCatalog
from kedro.pipeline import Pipeline
from kedro.pipeline.node import Node


class AbstractRunner(ABC):
    """``AbstractRunner`` is the base class for all ``Pipeline`` runner
    implementations.
    """

    @property
    def _logger(self):
        return logging.getLogger(self.__module__)

    def run(self, pipeline: Pipeline, catalog: DataCatalog) -> Dict[str, Any]:
        """Run the ``Pipeline`` using the ``DataSet``s provided by ``catalog``
        and save results back to the same objects.

        Args:
            pipeline: The ``Pipeline`` to run.
            catalog: The ``DataCatalog`` from which to fetch data.

        Raises:
            ValueError: Raised when ``Pipeline`` inputs cannot be satisfied.

        Returns:
            Any node outputs that cannot be processed by the ``DataCatalog``.
            These are returned in a dictionary, where the keys are defined
            by the node outputs.

        """

        catalog = catalog.shallow_copy()

        unsatisfied = pipeline.inputs() - set(catalog.list())
        if unsatisfied:
            raise ValueError(
                "Pipeline input(s) {} not found in the "
                "DataCatalog".format(unsatisfied)
            )

        free_outputs = pipeline.outputs() - set(catalog.list())
        unregistered_ds = pipeline.data_sets() - set(catalog.list())
        for ds_name in unregistered_ds:
            catalog.add(ds_name, self.create_default_data_set(ds_name))

        for ds_name in pipeline.all_inputs():
            num_loads = len(pipeline.only_nodes_with_inputs(ds_name).nodes)
            catalog.set_remaining_loads(ds_name, num_loads)

        self._run(pipeline, catalog)

        self._logger.info("Pipeline execution completed successfully.")

        return {ds_name: catalog.load(ds_name) for ds_name in free_outputs}

    def run_only_missing(
        self, pipeline: Pipeline, catalog: DataCatalog
    ) -> Dict[str, Any]:
        """Run only the missing outputs from the ``Pipeline`` using the
        ``DataSet``s provided by ``catalog`` and save results back to the same
        objects.

        Args:
            pipeline: The ``Pipeline`` to run.
            catalog: The ``DataCatalog`` from which to fetch data.
        Raises:
            ValueError: Raised when ``Pipeline`` inputs cannot be satisfied.

        Returns:
            Any node outputs that cannot be processed by the ``DataCatalog``.
            These are returned in a dictionary, where the keys are defined
            by the node outputs.

        """
        free_outputs = pipeline.outputs() - set(catalog.list())
        missing = {ds for ds in catalog.list() if not catalog.exists(ds)}
        to_build = free_outputs | missing
        to_rerun = pipeline.only_nodes_with_outputs(*to_build) + pipeline.from_inputs(
            *to_build
        )

        # we also need any memory data sets that feed into that
        # including chains of memory data sets
        memory_sets = pipeline.data_sets() - set(catalog.list())
        output_to_memory = pipeline.only_nodes_with_outputs(*memory_sets)
        input_from_memory = to_rerun.inputs() & memory_sets
        to_rerun += output_to_memory.to_outputs(*input_from_memory)

        return self.run(to_rerun, catalog)

    @abstractmethod  # pragma: no cover
    def _run(self, pipeline: Pipeline, catalog: DataCatalog) -> None:
        """The abstract interface for running pipelines, assuming that the
        inputs have already been checked and normalized by run().

        Args:
            pipeline: The ``Pipeline`` to run.
            catalog: The ``DataCatalog`` from which to fetch data.

        """
        pass

    @abstractmethod  # pragma: no cover
    def create_default_data_set(self, ds_name: str) -> AbstractDataSet:
        """Factory method for creating the default data set for the runner.

        Args:
            ds_name: Name of the missing data set

        Returns:
            An instance of an implementation of AbstractDataSet to be
            used for all unregistered data sets.

        """
        pass


def run_node(node: Node, catalog: DataCatalog) -> Node:
    """Run a single `Node` with inputs from and outputs to the `catalog`.

    Args:
        node: The ``Node`` to run.
        catalog: A ``DataCatalog`` containing the node's inputs and outputs.

    Returns:
        The node argument.

    """
    inputs = {name: catalog.load(name) for name in node.inputs}
    outputs = node.run(inputs)
    for name, data in outputs.items():
        catalog.save(name, data)
    return node
