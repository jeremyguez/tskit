#
# MIT License
#
# Copyright (c) 2020 Tskit Developers
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
Module for ranking and unranking trees. Trees are considered only
leaf-labelled and unordered, so order of children does not influence equality.
"""
import collections
import functools
import heapq
import itertools
import json

import numpy as np

import tskit


def treeseq_count_topologies(ts, sample_sets):
    topology_counter = np.full(ts.num_nodes, None, dtype=object)
    parent = np.full(ts.num_nodes, -1)

    def update_state(tree, u):
        stack = [u]
        while len(stack) > 0:
            v = stack.pop()
            children = []
            for c in tree.children(v):
                if topology_counter[c] is not None:
                    children.append(topology_counter[c])
            if len(children) > 0:
                topology_counter[v] = combine_child_topologies(children)
            else:
                topology_counter[v] = None
            p = parent[v]
            if p != -1:
                stack.append(p)

    for sample_set_index, sample_set in enumerate(sample_sets):
        for u in sample_set:
            if not ts.node(u).is_sample():
                raise ValueError(f"Node {u} in sample_sets is not a sample.")
            topology_counter[u] = TopologyCounter.from_sample(sample_set_index)

    for tree, (_, edges_out, edges_in) in zip(ts.trees(), ts.edge_diffs()):
        # Avoid recomputing anything for the parent until all child edges
        # for that parent are inserted/removed
        for p, sibling_edges in itertools.groupby(edges_out, key=lambda e: e.parent):
            for e in sibling_edges:
                parent[e.child] = -1
            update_state(tree, p)
        for p, sibling_edges in itertools.groupby(edges_in, key=lambda e: e.parent):
            if tree.is_sample(p):
                raise ValueError("Internal samples not supported.")
            for e in sibling_edges:
                parent[e.child] = p
            update_state(tree, p)

        counters = []
        for root in tree.roots:
            if topology_counter[root] is not None:
                counters.append(topology_counter[root])
        yield TopologyCounter.merge(counters)


def tree_count_topologies(tree, sample_sets):
    for u in tree.samples():
        if not tree.is_leaf(u):
            raise ValueError("Internal samples not supported.")

    topology_counter = np.full(tree.tree_sequence.num_nodes, None, dtype=object)
    for sample_set_index, sample_set in enumerate(sample_sets):
        for u in sample_set:
            if not tree.is_sample(u):
                raise ValueError(f"Node {u} in sample_sets is not a sample.")
            topology_counter[u] = TopologyCounter.from_sample(sample_set_index)

    for u in tree.nodes(order="postorder"):
        children = []
        for v in tree.children(u):
            if topology_counter[v] is not None:
                children.append(topology_counter[v])
        if len(children) > 0:
            topology_counter[u] = combine_child_topologies(children)

    counters = []
    for root in tree.roots:
        if topology_counter[root] is not None:
            counters.append(topology_counter[root])
    return TopologyCounter.merge(counters)


def combine_child_topologies(topology_counters):
    """
    Select all combinations of topologies from different
    counters in ``topology_counters`` that are capable of
    being combined into a single topology. This includes
    any combination of at least two topologies, all from
    different children, where no topologies share a
    sample set index.
    """
    partial_topologies = PartialTopologyCounter()
    for tc in topology_counters:
        partial_topologies.add_sibling_topologies(tc)

    return partial_topologies.join_all_combinations()


class TopologyCounter:
    """
    Contains the distributions of embedded topologies for every combination
    of the sample sets used to generate the ``TopologyCounter``. It is
    indexable by a combination of sample set indexes and returns a
    ``collections.Counter`` whose keys are topology ranks
    (see :ref:`sec_tree_ranks`). See :meth:`Tree.count_topologies` for more
    detail on how this structure is used.
    """

    def __init__(self):
        self.topologies = collections.defaultdict(collections.Counter)

    def __getitem__(self, sample_set_indexes):
        k = TopologyCounter._to_key(sample_set_indexes)
        return self.topologies[k]

    def __setitem__(self, sample_set_indexes, counter):
        k = TopologyCounter._to_key(sample_set_indexes)
        self.topologies[k] = counter

    @staticmethod
    def _to_key(sample_set_indexes):
        if not isinstance(sample_set_indexes, collections.abc.Iterable):
            sample_set_indexes = (sample_set_indexes,)
        return tuple(sorted(sample_set_indexes))

    def __eq__(self, other):
        return self.__class__ == other.__class__ and self.topologies == other.topologies

    @staticmethod
    def merge(topology_counters):
        """
        Union together independent topology counters into one.
        """
        total = TopologyCounter()
        for tc in topology_counters:
            for k, v in tc.topologies.items():
                total.topologies[k] += v

        return total

    @staticmethod
    def from_sample(sample_set_index):
        """
        Generate the topologies covered by a single sample. This
        is the single-leaf topology representing the single sample
        set.
        """
        rank_tree = RankTree(children=[], label=sample_set_index)
        tc = TopologyCounter()
        tc[sample_set_index][rank_tree.rank()] = 1
        return tc


class PartialTopologyCounter:
    """
    Represents the possible combinations of children under a node in a tree
    and the combinations of embedded topologies that are rooted at the node.
    This allows an efficient way of calculating which unique embedded
    topologies arise by only every storing a given pairing of sibling topologies
    once.
    ``partials`` is a dictionary where a key is a tuple of sample set indexes,
    and the value is a ``collections.Counter`` that counts combinations of
    sibling topologies whose tips represent the sample sets in the key.
    Each element of the counter is a homogeneous tuple where each element represents
    a topology. The topology is itself a tuple of the sample set indexes in that
    topology and the rank.
    """

    def __init__(self):
        self.partials = collections.defaultdict(collections.Counter)

    def add_sibling_topologies(self, topology_counter):
        """
        Combine each topology in the given TopologyCounter with every existing
        combination of topologies whose sample set indexes are disjoint from the
        topology from the counter. This also includes adding the topologies from
        the counter without joining them to any existing combinations.
        """
        merged = collections.defaultdict(collections.Counter)
        for sample_set_indexes, topologies in topology_counter.topologies.items():
            for rank, count in topologies.items():
                topology = ((sample_set_indexes, rank),)
                # Cross with existing topology combinations
                for sibling_sample_set_indexes, siblings in self.partials.items():
                    if isdisjoint(sample_set_indexes, sibling_sample_set_indexes):
                        for sib_topologies, sib_count in siblings.items():
                            merged_topologies = merge_tuple(sib_topologies, topology)
                            merged_sample_set_indexes = merge_tuple(
                                sibling_sample_set_indexes, sample_set_indexes
                            )
                            merged[merged_sample_set_indexes][merged_topologies] += (
                                count * sib_count
                            )
                # Propagate without combining
                merged[sample_set_indexes][topology] += count

        for sample_set_indexes, counter in merged.items():
            self.partials[sample_set_indexes] += counter

    def join_all_combinations(self):
        """
        For each pairing of child topologies, join them together into a new
        tree and count the resulting topologies.
        """
        topology_counter = TopologyCounter()
        for sample_set_indexes, sibling_topologies in self.partials.items():
            for topologies, count in sibling_topologies.items():
                # A node must have at least two children
                if len(topologies) >= 2:
                    rank = PartialTopologyCounter.join_topologies(topologies)
                    topology_counter[sample_set_indexes][rank] += count
                else:
                    # Pass on the single tree without adding a parent
                    for _, rank in topologies:
                        topology_counter[sample_set_indexes][rank] += count

        return topology_counter

    @staticmethod
    def join_topologies(child_topologies):
        children = []
        for sample_set_indexes, rank in child_topologies:
            n = len(sample_set_indexes)
            t = RankTree.unrank(n, rank, list(sample_set_indexes))
            children.append(t)
        children.sort(key=RankTree.canonical_order)
        return RankTree(children).rank()


def all_trees(num_leaves, span=1):
    """
    Generates all unique leaf-labelled trees with ``num_leaves``
    leaves. See :ref:`sec_combinatorics` on the details of this
    enumeration. The leaf labels are selected from the set
    ``[0, num_leaves)``. The times and labels on internal nodes are
    chosen arbitrarily.

    :param int num_leaves: The number of leaves of the tree to generate.
    :param float span: The genomic span of each returned tree.
    :rtype: tskit.Tree
    """
    for rank_tree in RankTree.all_labelled_trees(num_leaves):
        yield rank_tree.to_tsk_tree(span=span)


def all_tree_shapes(num_leaves, span=1):
    """
    Generates all unique shapes of trees with ``num_leaves`` leaves.

    :param int num_leaves: The number of leaves of the tree to generate.
    :param float span: The genomic span of each returned tree.
    :rtype: tskit.Tree
    """
    for rank_tree in RankTree.all_unlabelled_trees(num_leaves):
        default_labelling = rank_tree.label_unrank(0)
        yield default_labelling.to_tsk_tree(span=span)


def all_tree_labellings(tree, span=1):
    """
    Generates all unique labellings of the leaves of a
    :class:`tskit.Tree`. Leaves are labelled from the set
    ``[0, n)`` where ``n`` is the number of leaves of ``tree``.

    :param tskit.Tree tree: The tree used to generate
        labelled trees of the same shape.
    :param float span: The genomic span of each returned tree.
    :rtype: tskit.Tree
    """
    rank_tree = RankTree.from_tsk_tree(tree)
    for labelling in RankTree.all_labellings(rank_tree):
        yield labelling.to_tsk_tree(span=span)


class RankTree:
    """
    A tree class that maintains the topological ranks of each node in the tree.
    This structure can be used to efficiently compute the rank of a tree of
    n labelled leaves and produce a tree given a rank.
    """

    def __init__(self, children, label=None):
        # Children are assumed to be sorted by RankTree.canonical_order
        self.children = children
        if len(children) == 0:
            self.num_leaves = 1
            self.labels = [label]
        else:
            self.num_leaves = sum(c.num_leaves for c in children)
            self.labels = list(heapq.merge(*(c.labels for c in children)))

        self._shape_rank = None
        self._label_rank = None

    def compute_shape_rank(self):
        """
        Mirroring the way in which unlabelled trees are enumerated, we must
        first calculate the number of trees whose partitions of number of leaves
        rank lesser than this tree's partition.

        Once we reach the partition of leaves in this tree, we examine the
        groups of child subtrees assigned to subsequences of the partition.
        For each group of children with the same number of leaves, k, the trees
        in that group were selected according to a combination with replacement
        of those trees from S(k). By finding the rank of that combination,
        we find how many combinations preceded the current one in that group.
        That rank is then multiplied by the total number of arrangements that
        could be made in the following groups, added to the total rank,
        and then we recur on the rest of the group and groups.
        """
        part = self.leaf_partition()
        total = 0
        for prev_part in partitions(self.num_leaves):
            if prev_part == part:
                break
            total += num_tree_pairings(prev_part)

        child_groups = self.group_children_by_num_leaves()
        next_child_idx = 0
        for g in child_groups:
            next_child_idx += len(g)
            k = g[0].num_leaves
            S_k = num_shapes(k)

            child_ranks = [c.shape_rank() for c in g]
            g_rank = Combination.with_replacement_rank(child_ranks, S_k)

            # TODO precompute vector before loop
            rest_part = part[next_child_idx:]
            total_rest = num_tree_pairings(rest_part)

            total += g_rank * total_rest

        return total

    def compute_label_rank(self):
        """
        Again mirroring how we've labeled a particular tree, T, we can rank the
        labelling on T.

        We group the children into symmetric groups. In the context of labelling,
        symmetric groups contain child trees that are of the same shape. Each
        group contains a combination of labels selected from all the labels
        available to T.

        The different variables to consider are:
        1. How to assign a combination of labels to the first group.
        2. Given a combination of labels assigned to the group, how can we
            distribute those labels to each tree in the group.
        3. Given an assignment of the labels to each tree in the group, how many
            distinct ways could all the trees in the group be labelled.

        These steps for generating labelled trees break down the stages of
        ranking them.
        For each group G, we can find the rank of the combination of labels
        assigned to G. This rank times the number of ways the trees in G
        could be labelled, times the number of possible labellings of the
        rest of the trees, gives the number of labellings that precede those with
        the given combination of labels assigned to G. This process repeats and
        breaks down to give the rank of the assignment of labels to trees in G,
        and the label ranks of the trees themselves in G.
        """
        all_labels = self.labels
        child_groups = self.group_children_by_shape()
        total = 0
        for i, g in enumerate(child_groups):
            rest_groups = child_groups[i + 1 :]
            g_labels = list(heapq.merge(*(t.labels for t in g)))
            num_rest_labellings = num_list_of_group_labellings(rest_groups)

            # Preceded by all of the ways to label all the groups
            # with a lower ranking combination given to g.
            comb_rank = Combination.rank(g_labels, all_labels)
            num_g_labellings = num_group_labellings(g)
            preceding_comb = comb_rank * num_g_labellings * num_rest_labellings

            # Preceded then by all the configurations of g ranking less than
            # the current one
            rank_from_g = group_rank(g) * num_rest_labellings

            total += preceding_comb + rank_from_g
            all_labels = set_minus(all_labels, g_labels)

        return total

    # TODO I think this would boost performance if it were a field and not
    # recomputed.
    def num_labellings(self):
        child_groups = self.group_children_by_shape()
        return num_list_of_group_labellings(child_groups)

    def rank(self):
        return self.shape_rank(), self.label_rank()

    def shape_rank(self):
        if self._shape_rank is None:
            self._shape_rank = self.compute_shape_rank()
        return self._shape_rank

    def label_rank(self):
        if self._label_rank is None:
            assert self.shape_rank() is not None
            self._label_rank = self.compute_label_rank()
        return self._label_rank

    @staticmethod
    def unrank(num_leaves, rank, labels=None):
        """
        Produce a ``RankTree`` of the given ``rank`` with ``num_leaves`` leaves,
        labelled with ``labels``. Labels must be sorted, and if ``None`` default
        to ``[0, num_leaves)``.
        """
        shape_rank, label_rank = rank
        if shape_rank < 0 or label_rank < 0:
            raise ValueError("Rank is out of bounds.")
        unlabelled = RankTree.shape_unrank(num_leaves, shape_rank)
        return unlabelled.label_unrank(label_rank, labels)

    @staticmethod
    def shape_unrank(n, shape_rank):
        """
        Generate an unlabelled tree with n leaves with a shape corresponding to
        the `shape_rank`.
        """
        part, child_shape_ranks = children_shape_ranks(shape_rank, n)
        children = [
            RankTree.shape_unrank(k, rk) for k, rk in zip(part, child_shape_ranks)
        ]

        t = RankTree(children=children)
        t._shape_rank = shape_rank
        return t

    def label_unrank(self, label_rank, labels=None):
        """
        Generate a tree with the same shape, whose leaves are labelled
        from ``labels`` with the labelling corresponding to ``label_rank``.
        """
        if labels is None:
            labels = list(range(self.num_leaves))

        if self.is_leaf():
            if label_rank != 0:
                raise ValueError("Rank is out of bounds.")
            return RankTree(children=[], label=labels[0])

        child_groups = self.group_children_by_shape()
        child_labels, child_label_ranks = children_label_ranks(
            child_groups, label_rank, labels
        )

        children = self.children
        labelled_children = [
            RankTree.label_unrank(c, c_rank, c_labels)
            for c, c_rank, c_labels in zip(children, child_label_ranks, child_labels)
        ]

        t = RankTree(children=labelled_children)
        t._shape_rank = self.shape_rank()
        t._label_rank = label_rank
        return t

    @staticmethod
    def canonical_order(c):
        """
        Defines the canonical ordering of sibling subtrees.
        """
        return c.num_leaves, c.shape_rank(), c.min_label()

    @staticmethod
    def from_tsk_tree_node(tree, u):
        if tree.is_leaf(u):
            return RankTree(children=[], label=u)

        if tree.num_children(u) == 1:
            raise ValueError("Cannot rank trees with unary nodes")

        children = list(
            sorted(
                (RankTree.from_tsk_tree_node(tree, c) for c in tree.children(u)),
                key=RankTree.canonical_order,
            )
        )
        return RankTree(children=children)

    @staticmethod
    def from_tsk_tree(tree):
        if tree.num_roots != 1:
            raise ValueError("Cannot rank trees with multiple roots")

        return RankTree.from_tsk_tree_node(tree, tree.root)

    def to_tsk_tree(self, span=1):
        """
        Convert a ``RankTree`` into the only tree in a new tree sequence.

        :param float span: The genomic span of the returned tree. The tree will cover
            the interval :math:`[0, span)` and the :attr:`~Tree.tree_sequence` from which
            the tree is taken will have its :attr:`~tskit.TreeSequence.sequence_length`
            equal to ``span``.
        """
        if set(self.labels) != set(range(self.num_leaves)):
            raise ValueError("Labels set must be equivalent to [0, num_leaves)")

        tables = tskit.TableCollection(span)

        def add_node(node):
            if node.is_leaf():
                assert node.label is not None
                return node.label

            child_ids = [add_node(child) for child in node.children]
            # Arbitrarily set parent time +1 from their oldest child
            max_child_time = max(tables.nodes.time[c] for c in child_ids)
            parent_id = tables.nodes.add_row(time=max_child_time + 1)
            for child_id in child_ids:
                tables.edges.add_row(0, span, parent_id, child_id)

            return parent_id

        for _ in range(self.num_leaves):
            tables.nodes.add_row(flags=tskit.NODE_IS_SAMPLE, time=0)
        add_node(self)

        # The way in which we're inserting nodes doesn't necessarily
        # adhere to the ordering constraint on edges, so we have
        # to sort.
        tables.sort()
        return tables.tree_sequence().first()

    @staticmethod
    def all_labelled_trees(n):
        """
        Generate all unordered, leaf-labelled trees with n leaves.
        """
        for tree in RankTree.all_unlabelled_trees(n):
            yield from RankTree.all_labellings(tree)

    @staticmethod
    def all_unlabelled_trees(n):
        """
        Generate all tree shapes with n leaves. See :ref:`sec_combinatorics`
        for how tree shapes are enumerated.
        """
        if n == 1:
            yield RankTree(children=[])
        else:
            for part in partitions(n):
                for subtree_pairing in RankTree.all_subtree_pairings(
                    group_partition(part)
                ):
                    yield RankTree(children=subtree_pairing)

    @staticmethod
    def all_subtree_pairings(grouped_part):
        if len(grouped_part) == 0:
            yield []
        else:
            g = grouped_part[0]
            k = g[0]
            all_k_leaf_trees = RankTree.all_unlabelled_trees(k)
            num_k_leaf_trees = len(g)
            g_trees = itertools.combinations_with_replacement(
                all_k_leaf_trees, num_k_leaf_trees
            )
            for first_trees in g_trees:
                for rest in RankTree.all_subtree_pairings(grouped_part[1:]):
                    yield list(first_trees) + rest

    @staticmethod
    def all_labellings(tree, labels=None):
        """
        Given a tree, generate all the unique labellings of that tree.
        See :ref:`sec_combinatorics` for how labellings of a tree are
        enumerated.
        """
        if labels is None:
            labels = list(range(tree.num_leaves))

        if tree.is_leaf():
            assert len(labels) == 1
            yield RankTree(children=[], label=labels[0])
        else:
            groups = tree.group_children_by_shape()
            for labeled_children in RankTree.label_all_groups(groups, labels):
                yield RankTree(children=labeled_children)

    @staticmethod
    def label_all_groups(groups, labels):
        if len(groups) == 0:
            yield []
        else:
            g, rest = groups[0], groups[1:]
            x = len(g)
            k = g[0].num_leaves
            for g_labels in itertools.combinations(labels, x * k):
                rest_labels = set_minus(labels, g_labels)
                for labeled_g in RankTree.label_tree_group(g, g_labels):
                    for labeled_rest in RankTree.label_all_groups(rest, rest_labels):
                        yield labeled_g + labeled_rest

    @staticmethod
    def label_tree_group(trees, labels):
        if len(trees) == 0:
            assert len(labels) == 0
            yield []
        else:
            first, rest = trees[0], trees[1:]
            k = first.num_leaves
            min_label = labels[0]
            for first_other_labels in itertools.combinations(labels[1:], k - 1):
                first_labels = [min_label] + list(first_other_labels)
                rest_labels = set_minus(labels, first_labels)
                for labeled_first in RankTree.all_labellings(first, first_labels):
                    for labeled_rest in RankTree.label_tree_group(rest, rest_labels):
                        yield [labeled_first] + labeled_rest

    def newick(self):
        if self.is_leaf():
            return str(self.label) if self.labelled() else ""
        return "(" + ",".join(c.newick() for c in self.children) + ")"

    @property
    def label(self):
        return self.labels[0]

    def labelled(self):
        return all(label is not None for label in self.labels)

    def min_label(self):
        return self.labels[0]

    def is_leaf(self):
        return len(self.children) == 0

    def leaf_partition(self):
        return [c.num_leaves for c in self.children]

    def group_children_by_num_leaves(self):
        def same_num_leaves(c1, c2):
            return c1.num_leaves == c2.num_leaves

        return group_by(self.children, same_num_leaves)

    def group_children_by_shape(self):
        def same_shape(c1, c2):
            return c1.num_leaves == c2.num_leaves and c1.shape_rank() == c2.shape_rank()

        return group_by(self.children, same_shape)

    def __eq__(self, other):
        if self.__class__ != other.__class__:
            return False

        if self.is_leaf() and other.is_leaf():
            return self.label == other.label

        if len(self.children) != len(other.children):
            return False

        return all(c1 == c2 for c1, c2 in zip(self.children, other.children))

    def __ne__(self, other):
        return not self.__eq__(other)

    def shape_equal(self, other):
        if self.is_leaf() and other.is_leaf():
            return True

        if len(self.children) != len(other.children):
            return False

        return all(c1.shape_equal(c2) for c1, c2 in zip(self.children, other.children))

    def is_canonical(self):
        if self.is_leaf():
            return True

        children = self.children
        for c1, c2 in zip(children, children[1:]):
            if RankTree.canonical_order(c1) > RankTree.canonical_order(c2):
                return False
        return all(c.is_canonical() for c in children)

    def is_symmetrical(self):
        if self.is_leaf():
            return True

        even_split_leaves = len(set(self.leaf_partition())) == 1
        all_same_rank = len({c.shape_rank() for c in self.children}) == 1

        return even_split_leaves and all_same_rank


def randomly_resolve_polytomy(parent, children, intermediate_nodes, rng):
    """
    Take a parent node id and the :math:`n` node ids of its direct childen, and return
    a list of new [parent, child] relationships in which intermediate nodes have been
    inserted. This list of relationships describes a bifurcating tree whose root is the
    original parent node id and whose tips are the original children. The random
    algorithm that determines the insertion of the intermediate nodes results in the
    returned topology being chosen with equal probability from all possible bifurcating
    topologies for a tree with :math:`n` leaves. This is acheived by building
    up the tree from scratch by iterative addition of new edges.

    :param int parent: A parent node id.
    :param list children: A list of :math:`n` node ids specifying the immediate children
        of ``parent``. This function is only of any use when :math:`n > 2` (i.e. the
        ``parent`` node is a polytomy)
    :param list intermediate_nodes: A list of :math:`n-2` time-ordered new node ids
        which will appear as nodes "inserted" between the ``parent`` and ``children`` in
        the returned list. In order to ensure that the returned order has parents
        strictly older than their children, the ids should correspond to nodes that
        have non-identical times and should be listed in descending time order (greatest
        time first). Moreover, the time of the first intermediate node should be
        younger than the time of the passed-in ``parent``, and the time of the last
        intermediate node should be older than the oldest of the passed-in ``children``.
    :param int numpy.random.Generator: A numpy random number generator object, e.g. as
        returned by `np.random.default_rng(seed=...)`.

    :returns: a list of pairs of [parent, child] integers, which define a tree structure.
    :rtype: list
    """
    if len(children) != len(intermediate_nodes) + 2:
        raise ValueError("There must be two more children than intermedate nodes")
    # Polytomies broken by sequentially splicing onto edges, so an initial edge
    # is required. This will always remain above the top node & is removed later
    edges = [
        [None, children[0]],
    ]
    # We know beforehand how many random ints are needed: generate them all now
    edge_choice = rng.integers(0, np.arange(1, len(children) * 2 - 1, 2))
    tmp_lab = [parent] + intermediate_nodes
    assert len(edge_choice) == len(children) - 1
    for node_lab, child_id, target_edge_id in zip(tmp_lab, children[1:], edge_choice):
        target_edge = edges[target_edge_id]
        # Insert in the right place, to keep edges in parent time order
        edges.insert(target_edge_id, [node_lab, child_id])
        edges.insert(target_edge_id, [node_lab, target_edge[1]])
        target_edge[1] = node_lab
    top_edge = edges.pop()  # remove the edge above the top node
    assert top_edge[0] is None

    # Re-map the internal nodes IDs so they are used in time order
    real_node = iter(intermediate_nodes)
    node_map = {c: c for c in children}
    node_map[edges[-1][0]] = parent  # last edge == oldest parent
    for e in reversed(edges):
        # Reversing along the edges, parents are in inverse time order
        for idx in (0, 1):  # look at parent (0) then child (1)
            if e[idx] not in node_map:
                node_map[e[idx]] = next(real_node)
            e[idx] = node_map[e[idx]]
    assert len(node_map) == len(intermediate_nodes) + len(children) + 1
    return edges


def split_polytomies(
    ts,
    *,
    epsilon=None,
    method=None,
    record_provenance=True,
    random_seed=None,
):
    """
    Return a new tree sequence where extra nodes and edges have been inserted
    so that any any node ``u`` with greater than 2 children (i.e. a multifurcation
    or "polytomy") is resolved into successive bifurcations. Where ``method`` is
    "random" the newly generated bifurcating topologies will be produced equiprobably,
    by successive addition of edges to an initially empty list.

    For further documentation, please refer to the :meth:`TreeSequence.split_polytomies`
    method, which is the usual route through which this function is called.
    """
    allowed_methods = ["random"]
    if epsilon is None:
        epsilon = 1e-10
    if method is None:
        method = "random"
    if method not in allowed_methods:
        raise ValueError(f"Method must be chosen from {allowed_methods}")
    rng = np.random.default_rng(seed=random_seed)
    tables = ts.dump_tables()
    # Store existing table data before clearing
    old_edges_left = tables.edges.left.copy()  # Will be changed if the edge is split
    old_edges_right = tables.edges.right.copy()  # Read only copy for efficiency
    old_edges_parent = tables.edges.parent.copy()  # Read only copy for efficiency
    old_edges_child = tables.edges.child.copy()  # Read only copy for efficiency
    node_time = tables.nodes.time
    tables.edges.clear()

    edges_from_node = collections.defaultdict(set)  # Active descendant edge ids
    nodes_changed = set()

    for interval, e_out, e_in in ts.edge_diffs(include_terminal=True):
        for edge in itertools.chain(e_out, e_in):
            nodes_changed.add(edge.parent)
        pos = interval[0]
        for parent in nodes_changed:
            if len(edges_from_node[parent]) >= 3:
                child_edges = list(edges_from_node[parent])
                # We have a previous polytomy to break
                parent_time = node_time[parent]
                children = old_edges_child[child_edges]
                max_child_time = np.max(node_time[children])
                if epsilon > (parent_time - max_child_time) / (len(children) - 1):
                    min_val = (parent_time - max_child_time) / (len(children) - 1)
                    raise ValueError(
                        f"Epsilon={epsilon} not small enough to create new nodes under "
                        f"node {parent} just before pos {pos}: must be < {min_val}."
                    )
                left = old_edges_left[child_edges[0]]
                assert np.all(old_edges_left[child_edges] == left)
                # Split previous edges
                for edge_id in child_edges:
                    if old_edges_right[edge_id] > interval[0]:
                        # make sure we carry on the edge after this polytomy
                        old_edges_left[edge_id] = pos
                # Break this N-degree polytomy. This requires N-2 extra nodes to be
                # introduced: create them here in order of decreasing time
                new_nodes = [
                    tables.nodes.add_row(time=parent_time - (i * epsilon))
                    for i in range(1, len(children) - 1)
                ]
                for new_parent, new_child in randomly_resolve_polytomy(
                    parent, children, new_nodes, rng
                ):
                    tables.edges.add_row(
                        left=left, right=pos, parent=new_parent, child=new_child
                    )
            else:
                # Previous node was not a polytomy - just add the edges_out
                for edge_id in edges_from_node[parent]:
                    if old_edges_right[edge_id] == pos:  # is an out edge
                        tables.edges.add_row(
                            left=old_edges_left[edge_id],
                            right=pos,
                            parent=parent,
                            child=old_edges_child[edge_id],
                        )

        for edge in e_out:
            edges_from_node[edge.parent].remove(edge.id)
        for edge in e_in:
            edges_from_node[edge.parent].add(edge.id)

        # Chop if we have created a polytomy: the polytomy itself will be resolved
        # at a future iteration, when any edges move into or out of the polytomy
        while len(nodes_changed) > 0:
            node = nodes_changed.pop()
            edge_ids = edges_from_node[node]
            if len(edge_ids) == 0:
                del edges_from_node[node]
            # if this node has changed *to* a polytomy, we need to cut all of the
            # child edges that were previously present by adding the previous
            # segment and left-truncating
            elif len(edge_ids) >= 3:
                for edge_id in edge_ids:
                    if old_edges_left[edge_id] < interval[0]:
                        tables.edges.add_row(
                            left=old_edges_left[edge_id],
                            right=interval[0],
                            parent=old_edges_parent[edge_id],
                            child=old_edges_child[edge_id],
                        )
                    old_edges_left[edge_id] = interval[0]
    assert len(edges_from_node) == 0
    tables.sort()
    tables.edges.squash()
    tables.sort()  # Must re-sort, see https://github.com/tskit-dev/tskit/issues/808

    if record_provenance:
        parameters = {"command": "split_polytomies"}
        tables.provenances.add_row(
            record=json.dumps(tskit.provenance.get_provenance_dict(parameters))
        )
    try:
        return tables.tree_sequence()
    except tskit.LibraryError as e:
        if str(e).startswith(
            "A mutation's time must be < the parent node of the edge on which it occurs"
        ):
            e.args += (
                f"Epsilon={epsilon} not small enough to create new nodes below a "
                "polytomy, due to the time of a mutation above a child of the polytomy.",
            )
        raise e


# TODO This is called repeatedly in ranking and unranking and has a perfect
# subtructure for DP. It's only every called on n in [0, num_leaves]
# so we should compute a vector of those results up front instead of using
# repeated calls to this function.
# Put an lru_cache on for now as a quick replacement (cuts test time down by 80%)
@functools.lru_cache()
def num_shapes(n):
    """
    The cardinality of the set of unlabelled trees with n leaves,
    up to isomorphism.
    """
    if n <= 1:
        return n
    return sum(num_tree_pairings(part) for part in partitions(n))


def num_tree_pairings(part):
    """
    The number of unique tree shapes that could be assembled from
    a given partition of leaves. If we group the elements of the partition
    by number of leaves, each group can be independently enumerated and the
    cardinalities of each group's pairings can be multiplied. Within a group,
    subsequent trees must have equivalent or greater rank, so the number of
    ways to select trees follows combinations with replacement from the set
    of all possible trees for that group.
    """
    total = 1
    for g in group_partition(part):
        k = g[0]
        total *= Combination.comb_with_replacement(num_shapes(k), len(g))
    return total


def num_labellings(n, shape_rk):
    return RankTree.shape_unrank(n, shape_rk).num_labellings()


def children_shape_ranks(rank, n):
    """
    Return the partition of leaves associated
    with the children of the tree of rank `rank`, and
    the ranks of each child tree.
    """
    part = []
    for prev_part in partitions(n):
        num_trees_with_part = num_tree_pairings(prev_part)
        if rank < num_trees_with_part:
            part = prev_part
            break
        rank -= num_trees_with_part
    else:
        if n != 1:
            raise ValueError("Rank is out of bounds.")

    grouped_part = group_partition(part)
    child_ranks = []
    next_child = 0
    for g in grouped_part:
        next_child += len(g)
        k = g[0]

        # TODO precompute vector up front
        rest_children = part[next_child:]
        rest_num_pairings = num_tree_pairings(rest_children)

        shapes_comb_rank = rank // rest_num_pairings
        g_shape_ranks = Combination.with_replacement_unrank(
            shapes_comb_rank, num_shapes(k), len(g)
        )
        child_ranks += g_shape_ranks
        rank %= rest_num_pairings

    return part, child_ranks


def children_label_ranks(child_groups, rank, labels):
    """
    Produces the subsets of labels assigned to each child
    and the associated label rank of each child.
    """
    child_labels = []
    child_label_ranks = []

    for i, g in enumerate(child_groups):
        k = g[0].num_leaves
        g_num_leaves = k * len(g)
        num_g_labellings = num_group_labellings(g)
        # TODO precompute vector of partial products outside of loop
        rest_groups = child_groups[i + 1 :]
        num_rest_labellings = num_list_of_group_labellings(rest_groups)

        num_labellings_per_label_comb = num_g_labellings * num_rest_labellings
        comb_rank = rank // num_labellings_per_label_comb
        rank_given_label_comb = rank % num_labellings_per_label_comb
        g_rank = rank_given_label_comb // num_rest_labellings

        g_labels = Combination.unrank(comb_rank, labels, g_num_leaves)

        g_child_labels, g_child_ranks = group_label_ranks(g_rank, g, g_labels)
        child_labels += g_child_labels
        child_label_ranks += g_child_ranks

        labels = set_minus(labels, g_labels)
        rank %= num_rest_labellings

    return child_labels, child_label_ranks


def group_rank(g):
    k = g[0].num_leaves
    n = len(g) * k
    # Num ways to label a single one of the trees
    # We can do this once because all the trees in the group
    # are of the same shape rank
    y = g[0].num_labellings()
    all_labels = list(heapq.merge(*(t.labels for t in g)))
    rank = 0
    for i, t in enumerate(g):
        u_labels = t.labels
        curr_trees = len(g) - i
        # Kind of cheating here leaving the selection of min labels implicit
        # because the rank of the comb without min labels is the same
        comb_rank = Combination.rank(u_labels, all_labels)

        # number of ways to distribute labels to rest leaves
        num_rest_combs = 1
        remaining_leaves = n - (i + 1) * k
        for j in range(curr_trees - 1):
            num_rest_combs *= Combination.comb(remaining_leaves - j * k - 1, k - 1)

        preceding_combs = comb_rank * num_rest_combs * (y ** curr_trees)
        curr_comb = t.label_rank() * num_rest_combs * (y ** (curr_trees - 1))
        rank += preceding_combs + curr_comb
        all_labels = set_minus(all_labels, u_labels)
    return rank


# TODO This is only used in a few cases and mostly in a n^2 way. Would
# be easy and useful to do this DP and produce a list of partial products
def num_list_of_group_labellings(groups):
    """
    Given a set of labels and a list of groups, how many unique ways are there
    to assign subsets of labels to each group in the list and subsequently
    label all the trees in all the groups.
    """
    remaining_leaves = sum(len(g) * g[0].num_leaves for g in groups)
    total = 1
    for g in groups:
        k = g[0].num_leaves
        x = len(g)
        num_label_choices = Combination.comb(remaining_leaves, x * k)
        total *= num_label_choices * num_group_labellings(g)
        remaining_leaves -= x * k

    return total


def num_group_labellings(g):
    """
    Given a particular set of labels, how many unique ways are there
    to assign subsets of labels to each tree in the group and subsequently
    label those trees.
    """
    # Shortcut because all the trees are identical and can therefore
    # be labelled in the same ways
    num_tree_labelings = g[0].num_labellings() ** len(g)
    return num_assignments_in_group(g) * num_tree_labelings


def num_assignments_in_group(g):
    """
    Given this group of identical trees, how many unique ways
    are there to divide up a set of n labels?
    """
    n = sum(t.num_leaves for t in g)
    total = 1
    for t in g:
        k = t.num_leaves
        # Choose k - 1 from n - 1 because the minimum label must be
        # assigned to the first tree for a canonical labelling.
        total *= Combination.comb(n - 1, k - 1)
        n -= k
    return total


def group_label_ranks(rank, child_group, labels):
    """
    Given a group of trees of the same shape, a label rank and list of labels,
    produce assignment of label subsets to each tree in the group and the
    label rank of each tree.
    """
    child_labels = []
    child_label_ranks = []

    for i, rank_tree in enumerate(child_group):
        k = rank_tree.num_leaves
        num_t_labellings = rank_tree.num_labellings()
        rest_trees = child_group[i + 1 :]
        num_rest_assignments = num_assignments_in_group(rest_trees)
        num_rest_labellings = num_rest_assignments * (
            num_t_labellings ** len(rest_trees)
        )
        num_labellings_per_label_comb = num_t_labellings * num_rest_labellings

        comb_rank = rank // num_labellings_per_label_comb
        rank_given_comb = rank % num_labellings_per_label_comb
        t_rank = rank_given_comb // num_rest_labellings
        rank %= num_rest_labellings

        min_label = labels[0]
        t_labels = [min_label] + Combination.unrank(comb_rank, labels[1:], k - 1)
        labels = set_minus(labels, t_labels)

        child_labels.append(t_labels)
        child_label_ranks.append(t_rank)

    return child_labels, child_label_ranks


class Combination:
    @staticmethod
    def comb(n, k):
        """
        The number of times you can select k items from
        n items without order and without replacement.

        FIXME: This function will be available in `math` in Python 3.8
        and should be replaced eventually.
        """
        k = min(k, n - k)
        res = 1
        for i in range(1, k + 1):
            res *= n - k + i
            res //= i

        return res

    @staticmethod
    def comb_with_replacement(n, k):
        """
        Also called multichoose, the number of times you can select
        k items from n items without order but *with* replacement.
        """
        return Combination.comb(n + k - 1, k)

    @staticmethod
    def rank(combination, elements):
        """
        Find the combination of k elements from the given set of elements
        with the given rank in a lexicographic ordering.
        """
        indices = [elements.index(x) for x in combination]
        return Combination.from_range_rank(indices, len(elements))

    @staticmethod
    def from_range_rank(combination, n):
        """
        Find the combination of k integers from [0, n)
        with the given rank in a lexicographic ordering.
        """
        k = len(combination)
        if k == 0 or k == n:
            return 0

        j = combination[0]
        combination = [x - 1 for x in combination]
        if j == 0:
            return Combination.from_range_rank(combination[1:], n - 1)

        first_rank = Combination.comb(n - 1, k - 1)
        rest_rank = Combination.from_range_rank(combination, n - 1)
        return first_rank + rest_rank

    @staticmethod
    def unrank(rank, elements, k):
        n = len(elements)
        if k == 0:
            return []
        if len(elements) == 0:
            raise ValueError("Rank is out of bounds.")

        n_rest_combs = Combination.comb(n - 1, k - 1)
        if rank < n_rest_combs:
            return elements[:1] + Combination.unrank(rank, elements[1:], k - 1)

        return Combination.unrank(rank - n_rest_combs, elements[1:], k)

    @staticmethod
    def with_replacement_rank(combination, n):
        """
        Find the rank of ``combination`` in the lexicographic ordering of
        combinations with replacement of integers from [0, n).
        """
        k = len(combination)
        if k == 0:
            return 0
        j = combination[0]
        if k == 1:
            return j

        if j == 0:
            return Combination.with_replacement_rank(combination[1:], n)

        rest = [x - j for x in combination[1:]]
        preceding = 0
        for i in range(j):
            preceding += Combination.comb_with_replacement(n - i, k - 1)
        return preceding + Combination.with_replacement_rank(rest, n - j)

    @staticmethod
    def with_replacement_unrank(rank, n, k):
        """
        Find the combination with replacement of k integers from [0, n)
        with the given rank in a lexicographic ordering.
        """
        if k == 0:
            return []

        i = 0
        preceding = Combination.comb_with_replacement(n, k - 1)
        while rank >= preceding:
            rank -= preceding
            i += 1
            preceding = Combination.comb_with_replacement(n - i, k - 1)

        rest = Combination.with_replacement_unrank(rank, n - i, k - 1)
        return [i] + [x + i for x in rest]


def set_minus(arr, subset):
    return [x for x in arr if x not in set(subset)]


# TODO I think we can use part-count form everywhere. Right now
# there's a janky work-around of grouping the partition when
# we needed in part-count form but it doesn't look like there's any
# place that can't just accept it from the start.
def partitions(n):
    """
    Ascending integer partitions of n, excluding the partition [n].
    Since trees with unary nodes are uncountable, the partition of
    leaves must be at least size two.
    """
    if n > 0:
        # last partition is guaranteed to be length 1.
        yield from itertools.takewhile(lambda a: len(a) > 1, rule_asc(n))


def rule_asc(n):
    """
    Produce the integer partitions of n as ascending compositions.
    See: http://jeromekelleher.net/generating-integer-partitions.html
    """
    a = [0 for _ in range(n + 1)]
    k = 1
    a[1] = n
    while k != 0:
        x = a[k - 1] + 1
        y = a[k] - 1
        k -= 1
        while x <= y:
            a[k] = x
            y -= x
            k += 1
        a[k] = x + y
        yield a[: k + 1]


def group_by(values, equal):
    groups = []
    curr_group = []
    for x in values:
        if len(curr_group) == 0 or equal(x, curr_group[0]):
            curr_group.append(x)
        else:
            groups.append(curr_group)
            curr_group = [x]

    if len(curr_group) != 0:
        groups.append(curr_group)
    return groups


def group_partition(part):
    return group_by(part, lambda x, y: x == y)


def merge_tuple(tup1, tup2):
    return tuple(heapq.merge(tup1, tup2))


def isdisjoint(iterable1, iterable2):
    return set(iterable1).isdisjoint(iterable2)
