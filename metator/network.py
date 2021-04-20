#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Generate 3C networks of teh contig.
General utility functions for handling BAM files and generating 3C networks and 
some functions to work on the network once the bins are made to study some 
more specific contacts.

Core function to build the network are:
    - alignment_to_contacts
    - compute_contig_coverage
    - compute_network
    - create_contig_data
    - normalize_pair
    - precompute_network
    - write_contig_data

Some others functions to work on the network after the bins are made:
    - contigs_bin_network
    - merge_nodes
"""

import csv
import networkx as nx
import numpy as np
import pandas as pd
import re
from Bio import SeqIO
from Bio import SeqUtils
from os.path import join, basename
from metator.log import logger
import metator.io as mio


def alignment_to_contacts(
    aligment_files,
    assembly,
    depth_file,
    output_dir,
    output_file_network,
    output_file_contig_data,
    tmpdir,
    n_cpus,
    normalization,
    enzyme,
    self_contacts,
):
    """Generates a network file (in edgelist form) from an alignment. Contigs
    are the network nodes and the edges are the contact counts.

    The network is in a strict barebone form so that it can be reused and
    imported quickly into other applications etc. Verbose information about
    every single node in the network is written on a 'contig data' file, by
    default called 'idx_contig_length_GC_hit_cov.txt'

    Parameters
    ----------
    alignment_files : list of str
        List of path to the alignment file used as input.
    assembly : str
        The initial assembly path acting as the alignment file's reference
        assembly.
    depth_file : str or None
        Path to the depth.txt file from jgi_summarize_bam_contig_depths from
        Metabat2 Software.
    output_dir : str
        The output directory to write the network and chunk data into.
    output_file_network : str, optional
        The specific file name for the output network file. Default is
        'network.txt'
    output_file_contig_data : str, optional
        The specific file name for the output chunk data file. Default is
        'idx_contig_length_GC_hit_cov.txt'
    tmpdir : str
        Path to th temporary directory. Default in the working directory
    normalization : str
        If None, do not normalized the count of a contact by the geometric mean
        of the coverage of the contigs. Otherwise it's the type of
        normalization.
    enzyme : str or None
        String that contains the names of the enzyme separated by a comma.
    self_contacts : bool
        Whether to count alignments between a contig and
        itself. Default is False.

    Returns
    -------
    dict
        A dictionary where the keys are chunks in (contig, position) form and
        the values are their id, name, total contact count, size and coverage.
    """

    # Create temporary and output file which will be necessary
    precompute_network_file = join(tmpdir, "precompute_network_file.txt")
    network_file = join(output_dir, output_file_network)
    contig_data_file = join(output_dir, output_file_contig_data)
    hit_data_file = join(output_dir, "hit_data_alignment.txt")

    # Create the contig data dictionnary and hit from each alignments
    nb_alignment = len(aligment_files)
    contig_data, hit_data = create_contig_data(
        assembly, nb_alignment, depth_file, enzyme
    )

    # Create a contact file easily readable for counting the contacts.
    contig_data = precompute_network(
        aligment_files,
        contig_data,
        hit_data,
        precompute_network_file,
        self_contacts,
    )

    # Compute network
    compute_network(
        precompute_network_file,
        network_file,
        contig_data,
        tmpdir,
        n_cpus,
        normalization,
    )

    # Write the data from the contigs
    write_contig_data(contig_data, contig_data_file)
    if nb_alignment > 1:
        write_hit_data(hit_data, hit_data_file)

    return network_file, contig_data_file


def compute_contig_coverage(contig_data):
    """Compute the coverage of the contigs from the hit information compute
    previously

    Parameters:
    -----------
    contig_data : dict
        Dictionnary of the all the contigs from the assembly, the contigs names
        are the keys to the data of the contig available with the following
        keys: "id", "length", "GC", "hit", "coverage".

    Returns:
    --------
    dict:
        Dictionnary of the all the contigs from the assembly, the contigs names
        are the keys to the data of the contig available with the following
        keys: "id", "length", "GC", "hit", "coverage" with the coverage
        computed.

    Examples:
    ---------
    >>> compute_contig_coverage({"NODE_1" : {"id" : 1, "length" : 500, "GC" : 45.07, "hit" : 25, "coverage" : 0}, "NODE_2" : {"id" : 2, "length" : 100, "GC" : 45.07, "hit" : 200, "coverage" : 0}})
    {'NODE_1': {'id': 1, 'length': 500, 'GC': 45.07, 'hit': 25, 'coverage': 0.05}, 'NODE_2': {'id': 2, 'length': 100, 'GC': 45.07, 'hit': 200, 'coverage': 2.0}}
    """

    # Loop on the dictionnary and compute the coverage
    for name in contig_data:
        contig_data[name]["coverage"] = (
            contig_data[name]["hit"] * 1.0 / contig_data[name]["length"]
        )
    return contig_data


def compute_network(
    pre_network_file,
    network_file,
    contig_data,
    tmp_dir,
    n_cpus,
    normalization,
):
    """Generate the network file from the prenetwork file.

    Parameters:
    -----------
    pre_network_file  : str
        Path of the input file (prenetwork file, output from teh precompute
        network function)
    network_file : str
        Path of the output file (network file).
    contig_data : dict
        Dictionnary of the all the contigs from the assembly, the contigs names
        are the keys to the data of the contig available with the following
        keys: "id", "length", "GC", "hit", "coverage", "RS".
    tmp_dir : str
        Path of the temporary directory to write files.
    n_cpus : int
        Number of cpus used to sort the prenetwork.
    normalization : str
        If None, do not normalized the count of a contact by the geometric mean
        of the coverage of the contigs. Otherwise it's the type of
        normalization.
    """

    # Create a temporary directory for the sorted pre-network
    pre_network_sorted = join(tmp_dir, "tmp_network_sorted.txt")

    # Sort the the pre-network file
    mio.sort_pairs(
        pre_network_file,
        pre_network_sorted,
        tmp_dir=tmp_dir,
        threads=n_cpus,
    )

    # Set the variables used in the loop
    n_occ = 1  # Number of occurences of each frag combination
    n_nonzero = 1  # Total number of nonzero matrix entries
    n_pairs = 0  # Total number of pairs entered in the matrix

    # Read the sorted paors
    with open(pre_network_sorted, "r") as pairs, open(network_file, "w") as net:
        pairs_reader = csv.reader(pairs, delimiter="\t")
        prev_pair = next(pairs_reader)
        for pair in pairs_reader:
            # Extract the contig names and their id
            curr_pair = pair
            # Increment number of occurences for fragment pair
            if prev_pair == curr_pair:
                n_occ += 1
            # Write previous pair and start a new one
            else:
                if n_occ > 0:

                    # Normalisation using the geometric mean of the coverage
                    if normalization != "None":
                        effective_count = normalize_pair(
                            contig_data, prev_pair, n_occ, normalization
                        )
                    else:
                        effective_count = n_occ

                    # Write the line in the file
                    net.write(
                        "\t".join(
                            map(
                                str,
                                [
                                    contig_data[prev_pair[0]]["id"],
                                    contig_data[prev_pair[1]]["id"],
                                    effective_count,
                                ],
                            )
                        )
                        + "\n"
                    )
                # Reset the values for the next pair
                prev_pair = curr_pair
                n_pairs += n_occ
                n_occ = 1
                n_nonzero += 1

        # Write the last value
        if normalization != "None":
            effective_count = normalize_pair(
                contig_data, prev_pair, n_occ, normalization
            )
        else:
            effective_count = n_occ

        net.write(
            "\t".join(
                map(
                    str,
                    [
                        contig_data[curr_pair[0]]["id"],
                        contig_data[curr_pair[1]]["id"],
                        effective_count,
                    ],
                )
            )
            + "\n"
        )
        n_nonzero += 1
        n_pairs += n_occ


def normalize_pair(contig_data, prev_pair, n_occ, normalization):
    """
    contig_data : dict
        Dictionnary of the all the contigs from the assembly, the contigs names
        are the keys to the data of the contig available with the following
        keys: "id", "length", "GC", "hit", "coverage", "RS".
    """
    # The four first normalizations are normalization by the geometric mean of
    # "coverage".
    if normalization == "abundance":
        factor = np.sqrt(
            contig_data[prev_pair[0]]["coverage"]
            * contig_data[prev_pair[1]]["coverage"]
        )
        # If no read mapped from Shotgun libraries could be equal to zero.
        if factor == 0:
            return 0

    elif normalization == "length":
        factor = np.sqrt(
            contig_data[prev_pair[0]]["hit"]
            / contig_data[prev_pair[0]]["length"]
            * contig_data[prev_pair[1]]["hit"]
            / contig_data[prev_pair[1]]["length"]
        )

    elif normalization == "RS":
        factor = 0.01 * np.sqrt(
            contig_data[prev_pair[0]]["hit"]
            / contig_data[prev_pair[0]]["RS"]
            * contig_data[prev_pair[1]]["hit"]
            / contig_data[prev_pair[1]]["RS"]
        )

    elif normalization == "RS_length":
        factor = np.sqrt(
            contig_data[prev_pair[0]]["hit"]
            / 10
            * np.sqrt(
                contig_data[prev_pair[0]]["length"]
                * contig_data[prev_pair[0]]["RS"]
            )
            * contig_data[prev_pair[1]]["hit"]
            / 10
            * np.sqrt(
                contig_data[prev_pair[1]]["length"]
                * contig_data[prev_pair[1]]["RS"]
            )
        )

    # The last two normalizations are normalization by geometric means of hits.
    elif normalization == "empirical_hit":
        factor = np.sqrt(
            contig_data[prev_pair[0]]["hit"] * contig_data[prev_pair[1]]["hit"]
        )

    elif normalization == "theoritical_hit":
        factor = np.sqrt(
            contig_data[prev_pair[0]]["coverage"]
            * 10
            * np.sqrt(
                contig_data[prev_pair[0]]["length"]
                * contig_data[prev_pair[0]]["RS"]
            )
            * contig_data[prev_pair[1]]["coverage"]
            * 10
            * np.sqrt(
                contig_data[prev_pair[1]]["length"]
                * contig_data[prev_pair[1]]["RS"]
            )
        )
        # If no read mapped from Shotgun libraries could be equal to zero.
        if factor == 0:
            return 0

    return n_occ / factor


def create_contig_data(assembly, nb_alignment, depth_file, enzyme):
    """Create a dictionnary with data on each Contig.

    Paramaters
    ----------
    assembly : str
        Path to the assembly fasta file
    nb_alignement : int
        Numbers of alignment files.
    depth_file : str or None
        Path to the depth.txt file from jgi_summarize_bam_contig_depths from
        Metabat2 Software.
    enzyme : str or None
        String that contains the names of the enzyme separated by a comma.

    Return
    ------
    dict:
        Dictionnary of the all the contigs from the assembly, the contigs names
        are the keys to the data of the contig available with the following
        keys: "id", "length", "GC", "hit", "coverage", "RS". Hit is set to 0 and
        need to be updated later.
    dict:
        Dictionnary for hit information on each contigs.
    """

    # Create a contig data dictionnary from the assembly file
    contig_data = dict()
    if nb_alignment > 1:
        hit_data = dict()
    else:
        hit_data = None

    # Extract restriction sites if an enzyme is given.
    if enzyme:
        pattern = mio.get_restriction_site(enzyme)

    global_id = 1
    if depth_file:
        with open(depth_file, "r") as depth:
            line = depth.readline()
            for contig in SeqIO.parse(assembly, "fasta"):
                line = depth.readline().split("\t")
                contig_data[contig.id] = {
                    "id": global_id,
                    "length": int(line[1]),
                    "GC": SeqUtils.GC(contig.seq),
                    "hit": 0,
                    "coverage": float(line[2]),
                    "RS": (len(re.findall(pattern, str(contig.seq))) + 1)
                    if enzyme
                    else "-",
                }
                if nb_alignment > 1:
                    hit_data[contig.id] = {
                        "id": global_id,
                        "hit": [0] * nb_alignment,
                    }
                global_id += 1
    else:
        for contig in SeqIO.parse(assembly, "fasta"):
            contig_data[contig.id] = {
                "id": global_id,
                "length": len(contig.seq),
                "GC": SeqUtils.GC(contig.seq),
                "hit": 0,
                "coverage": "-",
                "RS": (len(re.findall(pattern, str(contig.seq))) + 1)
                if enzyme
                else "-",
            }
            if nb_alignment > 1:
                hit_data[contig.id] = {
                    "id": global_id,
                    "hit": [0] * nb_alignment,
                }
            global_id += 1

    return contig_data, hit_data


def precompute_network(
    aligment_files, contig_data, hit_data, out_file, self_contacts=False
):
    """Write a file with only the contig id separated by a tabulation and count
    the contacts by contigs to be able to compute directlty the normalized
    network.

    Parameters
    ----------
    alignment_files : str
        List of path to the alignment file.
    contig_data : dict
        Dictionnary of the all the contigs from the assembly, the contigs names
        are the keys to the data of the contig available with the following
        keys: "id", "length", "GC", "hit", "coverage". Coverage still at 0 and
        need to be updated later.
    hit_data : dict
        Dictionnary with the count of hits for each aligment file.
    out_file : str
        Path to the write the output_file which will be necessary to compute the
        network.
    self_contacts : bool
        If True, the contacts on the same contigs will be displayed. Otherwise
        only displays the inter contigs contacts.

    Return
    ------
    dict:
        Dictionnary of the all the contigs from the assembly, the contigs names
        are the keys to the data of the contig available with the following
        keys: "id", "length", "GC", "hit", "coverage". Coverage still at 0 and
        need to be updated later.
    """
    # Initiate value to compute 3D ratio
    all_contacts = 0
    inter_contacts = 0

    # Prepare a file to save contact with their global ID
    with open(out_file, "w") as pre_net:

        # Iterates on the alignment files
        for i, aligment_file in enumerate(aligment_files):

            all_contacts_temp = 0
            inter_contacts_temp = 0

            # Read the alignment_file and build pairs for the network
            with open(aligment_file, "r") as pairs:
                for pair in pairs:
                    # Split the line on the tabulation
                    p = pair.split("\t")

                    # Extract the contig names which are at the position 2 and 6.
                    contig1, contig2 = p[1], p[5]
                    id1 = contig_data[contig1]["id"]
                    id2 = contig_data[contig2]["id"]

                    # Count the contact
                    all_contacts_temp += 1
                    contig_data[contig1]["hit"] += 1
                    contig_data[contig2]["hit"] += 1
                    if len(aligment_files) > 1:
                        hit_data[contig1]["hit"][i] += 1
                        hit_data[contig2]["hit"][i] += 1

                    # Write the file used for the computation of the network.
                    if self_contacts and id1 == id2:
                        pre_net.write(
                            "\t".join(map(str, [contig1, contig2])) + "\n"
                        )
                    elif id1 < id2:
                        inter_contacts_temp += 1
                        pre_net.write(
                            "\t".join(map(str, [contig1, contig2])) + "\n"
                        )
                    elif id1 > id2:
                        inter_contacts_temp += 1
                        pre_net.write(
                            "\t".join(map(str, [contig2, contig1])) + "\n"
                        )

            # Count contacts and return sample informations.
            all_contacts += all_contacts_temp
            inter_contacts += inter_contacts_temp
            logger.info("Information of {0}:".format(basename(aligment_file)))
            logger.info(
                "{0} contacts in the library.".format(all_contacts_temp)
            )
            logger.info(
                "{0} contacts inter-contigs in the library.".format(
                    inter_contacts_temp
                )
            )
            logger.info(
                "3D ratio : {0}\n".format(
                    inter_contacts_temp / all_contacts_temp
                )
            )

    # Return information about the network
    logger.info("General information:")
    logger.info("{0} contacts in the library.".format(all_contacts))
    logger.info(
        "{0} contacts inter-contigs in the library.".format(inter_contacts)
    )
    logger.info("3D ratio : {0}\n".format(inter_contacts / all_contacts))

    return contig_data


def write_contig_data(contig_data, output_path):
    """Function to write the contig data file at the output path given. The file
    will contains 7 columns separated by a tabulation: id, name, length,
    GC_content, hit, coverage, restriction site for each contig.

    Parameters
    ----------
    contig_data : dict
        Dictionnary of the all the contigs from the assembly, the contigs names
        are the keys to the data of the contig available with the following
        keys: "id", "length", "GC", "hit", "coverage", "RS".
    output_path : str
        Path to the output file where the data from the dictionnary will be
        written
    """

    # For each contig extract the data and write them in the file.
    with open(output_path, "w") as contig_data_file_handle:
        line = "ID\tName\tSize\tGC_content\tHit\tShotgun_coverage\tRestriction_site\n"
        contig_data_file_handle.write(line)
        for name in contig_data:
            length = contig_data[name]["length"]
            hit = contig_data[name]["hit"]
            coverage = contig_data[name]["coverage"]
            GC_content = contig_data[name]["GC"]
            idx = contig_data[name]["id"]
            RS = contig_data[name]["RS"]
            line = "{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format(
                idx, name, length, GC_content, hit, coverage, RS
            )
            contig_data_file_handle.write(line)


def write_hit_data(hit_data, output_path):
    """Function to write the contig data file at the output path given. The file
    will contains 6 columns separated by a tabulation: id, name, length,
    GC_content, hit, coverage for each contig.

    Parameters
    ----------
    hit_data : dict
        Dictionnary of the all the contigs from the assembly, the contigs names
        are the keys to a list of hits from each alignment files separately.
    output_path : str
        Path to the output file where the data from the dictionnary will be
        written.
    """

    # For each contig extract the data and write them in the file.
    with open(output_path, "w") as hit_data_file_handle:
        for name in hit_data:
            hit_list = map(str, hit_data[name]["hit"])
            hit_str = "\t".join(hit_list)
            idx = hit_data[name]["id"]
            line = "{0}\t{1}\t{2}\n".format(idx, name, hit_str)
            hit_data_file_handle.write(line)


def contigs_bin_network(
    contigs_network_file,
    contigs_data_file,
    contigs_of_interest,
    bins_network_file,
    bin_size,
):
    """Build a network of the bins and contigs of interest from contigs network.

    Here group the contigs together from the bin information and build a new
    network of contact between the bins. If some contigs of interest are given
    such as contigs annotated as virus it will keep them independantly in the
    network.

    Parameters
    ----------
    contigs_network_file : str
        Path to the normalized contigs network file.
    contigs_data_file : str
        Path to the contig data file with all the attribution of each contigs to
        a bin.
    contigs_of_interest : str or None
        Path to a file containing contigs of interest. If set to None, the
        function will only generate the network of bins
    bins_network_file : str
        Path of the output file to write the output file containing the networks
        of the bins.
    bin_size : int
        Size of the bins in base pair to considered for the new networks.

    Returns
    -------
    networkx.classes.graph.Graph:
        The network of the bins of interest
    """

    # Import contigs data.
    contigs_data = pd.read_csv(
        contigs_data_file, sep="\t", header=None, index_col=False
    )
    contigs_data.columns = [
        "ID",
        "Name",
        "Size",
        "GC_content",
        "HiC_contact",
        "Shotgun_coverage",
        "Restriction_site" "core_comunitiy_id",
        "core_community_contigs",
        "core_community_size",
        "overlapping_comunitiy_id",
        "overlapping_community_contigs",
        "overlapping_community_size",
    ]

    # Import network of contigs.
    contigs_network = nx.read_edgelist(
        contigs_network_file, nodetype=int, data=(("weight", float),)
    )

    # Look for contigs of interest is some given
    if contigs_of_interest == None:
        contigs_of_interest = []
    else:
        contigs_of_interest = pd.read_csv(
            contigs_of_interest, header=None, index_col=False
        )
        contigs_of_interest.columns = ["name"]
        contigs_of_interest = list(
            contigs_of_interest.merge(contigs_data, on="name", how="inner")[
                "id"
            ]
        )

    # Look for the contigs list of each overlapping bin. The overlapping bin "0"
    # corresponds to the contigs alone after the binning.
    bin_list = dict()
    for contig_id in contigs_data["id"]:
        if contig_id not in contigs_of_interest:
            try:
                oc_id = contigs_data["oc_id"][contig_id - 1]
                if int(contigs_data["oc_length"][contig_id - 1]) > bin_size:
                    try:
                        bin_list[oc_id].append(contig_id)
                    except KeyError:
                        bin_list[oc_id] = [contig_id]
            except ValueError:
                oc_id = 0

    # Group network in overlapping communities.
    # Create a list of the nodes ID of the bin to keep at the end.
    to_keep = contigs_of_interest
    for bin_id in bin_list.keys():
        bin_node = "bin_" + str(bin_id)
        to_keep.append(bin_node)
        contigs_network = merge_nodes(
            contigs_network, bin_list[bin_id], bin_node
        )

    # Keep only bin nodes and remove all the contigs nodes not used.
    bins_network = contigs_network.subgraph(to_keep)

    # Write the new network file.
    nx.write_edgelist(
        bins_network, bins_network_file, delimiter="\t", data=["weight"]
    )

    return bins_network


def merge_nodes(G1, nodes, new_node):
    """Merges the selected `nodes` of the graph G into one `new_node`, meaning
    that all the edges that pointed to or from one of these `nodes` will point
    to or from the `new_node`. It will add the weigth of the edges if two edges
    become the same at the end of the merging.

    Parameters:
    -----------
    G1 : networkx.classes.graph.Graph
        Network where there are the nodes to merge.
    nodes : list
        List of the ids of the nodes to merge.
    new_node : str
        ID of the new node to create.

    Returns:
    --------
    networkx.classes.graph.Graph:
        Network with the nodes merged.
    """

    # Create a copy to avoid issues of creating new nodes and edges during the
    # loop.
    G2 = G1.copy()

    # Add the 'merged' node
    G2.add_node(new_node)

    for n1, n2, data in G1.edges(data=True):
        # For all edges related to one of the nodes to merge, make an edge going
        # to or coming from the `new node`. If the edge already exists make the
        # sum of the weigths
        if n1 in nodes:
            if G2.has_edge(n2, new_node):
                G2.edges[n2, new_node]["weight"] += data["weight"]
            else:
                G2.add_edge(n2, new_node, weight=data["weight"])
        elif n2 in nodes:
            if G2.has_edge(n1, new_node):
                G2.edges[n1, new_node]["weight"] += data["weight"]
            else:
                G2.add_edge(n1, new_node, weight=data["weight"])

    # remove the merged nodes
    for n in nodes:
        G2.remove_node(n)

    return G2
