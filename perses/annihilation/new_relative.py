import simtk.openmm as openmm
import simtk.openmm.app as app
import simtk.unit as unit
import numpy as np
import copy
import enum

InteractionGroup = enum.Enum("InteractionGroup", ['unique_old', 'unique_new', 'core', 'environment'])

class HybridTopologyFactory(object):
    """
    This class generates a hybrid topology based on a perses topology proposal. This class treats atoms
    in the resulting hybrid system as being from one of four classes:

    unique_old_atom : these atoms are not mapped and only present in the old system. Their interactions will be on for
        lambda=0, off for lambda=1
    unique_new_atom : these atoms are not mapped and only present in the new system. Their interactions will be off
        for lambda=0, on for lambda=1
    core_atom : these atoms are mapped, and are part of a residue that is changing. Their interactions will be those
        corresponding to the old system at lambda=0, and those corresponding to the new system at lambda=1
    environment_atom : these atoms are mapped, and are not part of a changing residue. Their interactions are always
        on and are alchemically unmodified.
    """

    def __init__(self, topology_proposal, current_positions, new_positions):
        """
        Initialize the Hybrid topology factory.

        Parameters
        ----------
        topology_proposal : perses.rjmc.topology_proposal.TopologyProposal object
            TopologyProposal object rendered by the ProposalEngine
        current_positions : [n,3] np.ndarray of float
            The positions of the "old system"
        new_positions : [m,3] np.ndarray of float
            The positions of the "new system"
        """
        self._topology_proposal = topology_proposal
        self._old_system = copy.deepcopy(topology_proposal.old_system)
        self._new_system = copy.deepcopy(topology_proposal.new_system)
        self._old_to_hybrid_map = {}
        self._new_to_hybrid_map = {}
        self._hybrid_system_forces = {}

        #prepare dicts of forces, which will be useful later
        self._old_system_forces = {type(force).__name__ : force for force in self._old_system.getForces()}
        self._new_system_forces = {type(force).__name__ : force for force in self._new_system.getForces()}

        #start by creating an empty system and topology. These will become the hybrid system and topology.
        self._hybrid_system = openmm.System()
        self._hybrid_topology = app.Topology()

        #begin by copying all particles in the old system to the hybrid system. Note that this does not copy the
        #interactions. It does, however, copy the particle masses. In general, hybrid index and old index should be
        #the same.
        for particle_idx in range(self._topology_proposal.natoms_old):
            particle_mass = self._old_system.getParticleMass(particle_idx)
            hybrid_idx = self._hybrid_system.addParticle(particle_mass)
            self._old_to_hybrid_map[particle_idx] = hybrid_idx

            #If the particle index in question is mapped, make sure to add it to the new to hybrid map as well.
            if particle_idx in self._topology_proposal.old_to_new_atom_map.keys():
                particle_index_in_new_system = self._topology_proposal.old_to_new_atom_map[particle_idx]
                self._new_to_hybrid_map[particle_index_in_new_system] = hybrid_idx

        #Next, add the remaining unique atoms from the new system to the hybrid system and map accordingly.
        #As before, this does not copy interactions, only particle indices and masses.
        for particle_idx in self._topology_proposal.unique_new_atoms:
            particle_mass = self._new_system.getParticleMass(particle_idx)
            hybrid_idx = self._hybrid_system.addParticle(particle_mass)
            self._new_to_hybrid_map[particle_idx] = hybrid_idx

        #assign atoms to one of the classes described in the class docstring
        self._atom_classes = self._determine_atom_classes()

        #verify that no constraints are changing over the course of the switching.
        self._constraint_check()

        #loop through the forces in the old system and begin to dispatch them to handlers that will add appropriate
        #force terms in the hybrid system. The scheme here is to always add all interactions (with appropriate lambda
        #terms) from the old system, and then only add unique new interactions from the new system.
        old_system_forces = self._topology_proposal.old_system.getForces()

        for force in old_system_forces:
            force_class_name = type(force).__name__
            if force_class_name=="HarmonicBondForce":
                #dispatch to bond force handler
                pass
            elif force_class_name=="HarmonicAngleForce":
                pass
                #dispatch to angle force handler
            elif force_class_name=="PeriodicTorsionForce":
                pass
                #dispatch to torsion force handler
            elif force_class_name=="NonbondedForce":
                pass
                #dispatch to nonbonded force handler
            else:
                raise ValueError("An unknown force class is present.")


    def _get_core_atoms(self):
        """
        Determine which atoms in the old system are part of the "core" class. All necessary information is contained in
        the topology proposal passed to the constructor.

        Returns
        -------
        core_atoms : set of int
            The set of atoms (hybrid topology indexed) that are core atoms.
        environment_atoms : set of int
            The set of atoms (hybrid topology indexed) that are environment atoms.
        """

        core_atoms = set()

        #In order to be either a core or environment atom, the atom must be mapped.
        mapped_old_atoms_set = set(self._topology_proposal.old_to_new_atom_map.keys())
        mapped_new_atoms_set = set(self._topology_proposal.old_to_new_atom_map.values())
        mapped_hybrid_atoms_set = {self._old_to_hybrid_map[atom_idx] for atom_idx in mapped_old_atoms_set}

        #create sets for set arithmetic
        unique_old_set = set(self._topology_proposal.unique_old_atoms)
        unique_new_set = set(self._topology_proposal.unique_new_atoms)

        #we derive core atoms from the old topology:
        core_atoms_from_old = self._determine_core_atoms_in_topology(self._topology_proposal.old_topology,
                                                                     unique_old_set, mapped_old_atoms_set,
                                                                     self._old_to_hybrid_map)

        #we also derive core atoms from the new topology:
        core_atoms_from_new = self._determine_core_atoms_in_topology(self._topology_proposal.new_topology,
                                                                     unique_new_set, mapped_new_atoms_set,
                                                                     self._new_to_hybrid_map)

        #The union of the two will give the core atoms that can result from either new or old topology
        total_core_atoms = core_atoms_from_old.union(core_atoms_from_new)

        #as a side effect, we can now compute the environment atom indices too, by subtracting the core indices
        #from the mapped atom set (since any atom that is mapped but not core is environment)
        environment_atoms = mapped_hybrid_atoms_set.difference(total_core_atoms)

        return total_core_atoms, environment_atoms

    def _determine_core_atoms_in_topology(self, topology, unique_atoms, mapped_atoms, hybrid_map):
        """
        Given a topology and its corresponding unique and mapped atoms, return the set of atom indices in the
        hybrid system which would belong to the "core" atom class

        Parameters
        ----------
        topology : simtk.openmm.app.Topology
            An OpenMM topology representing a system of interest
        unique_atoms : set of int
            A set of atoms that are unique to this topology
        mapped_atoms : set of int
            A set of atoms that are mapped to another topology

        Returns
        -------
        core_atoms : set of int
            set of core atom indices in hybrid topology
        """
        core_atoms = set()

        #loop through the residues to look for ones with unique atoms
        for residue in topology.residues():
            atom_indices_old_system = {atom.index for atom in residue.atoms()}

            #if the residue contains an atom index that is unique, then the residue is changing.
            #We determine this by checking if the atom indices of the residue have any intersection with the unique atoms
            if len(atom_indices_old_system.intersection(unique_atoms)) > 0:
                #we can add the atoms in this residue which are mapped to the core_atoms set:
                for atom_index in atom_indices_old_system:
                    if atom_index in mapped_atoms:
                        #we specifically want to add the hybrid atom.
                        hybrid_index = hybrid_map[atom_index]
                        core_atoms.add(hybrid_index)

        return core_atoms

    def _determine_atom_classes(self):
        """
        This method determines whether each atom belongs to unique old, unique new, core, or environment, as defined above.
        All the information required is contained in the TopologyProposal passed to the constructor. All indices are
        indices in the hybrid system.

        Returns
        -------
        atom_classes : dict of list
            A dictionary of the form {'core' :core_list} etc.
        """
        atom_classes = {'unique_old_atoms' : set(), 'unique_new_atoms' : set(), 'core_atoms' : set(), 'environment_atoms' : set()}

        #first, find the unique old atoms, as this is the most straightforward:
        for atom_idx in self._topology_proposal.unique_old_atoms:
            hybrid_idx = self._old_to_hybrid_map[atom_idx]
            atom_classes['unique_old_atoms'].add(hybrid_idx)

        #Then the unique new atoms (this is substantially the same as above)
        for atom_idx in self._topology_proposal.unique_new_atoms:
            hybrid_idx = self._new_to_hybrid_map[atom_idx]
            atom_classes['unique_new_atoms'].add(hybrid_idx)

        core_atoms, environment_atoms = self._get_core_atoms()

        atom_classes['core_atoms'] = core_atoms
        atom_classes['environment_atoms'] = environment_atoms

        return atom_classes

    def _constraint_check(self):
        """
        This is a check to make sure that constraint lengths do not change over the course of the switching.
        In the future, we will determine a method to deal with this. Raises exception if a constraint length changes.
        """

        #this dict will be of the form {(atom1, atom2) : constraint_value}, with hybrid indices.
        constrained_atoms_dict = {}

        #first, loop through constraints in the old system and add them to the dict, with hybrid indices:
        for constraint_idx in range(self._topology_proposal.old_system.getNumConstraints()):
            atom1, atom2, constraint = self._topology_proposal.old_system.getConstraintParameters(constraint_idx)
            atom1_hybrid = self._old_to_hybrid_map[atom1]
            atom2_hybrid = self._old_to_hybrid_map[atom2]
            constrained_atoms_dict[(atom1_hybrid, atom2_hybrid)] = constraint

        #now, loop through constraints in the new system, and see if we are going to change a constraint length
        for constraint_idx in range(self._topology_proposal.new_system.getNumConstraints()):
            atom1, atom2, constraint = self._topology_proposal.new_system.getConstraintParameters(constraint_idx)
            atom1_hybrid = self._new_to_hybrid_map[atom1]
            atom2_hybrid = self._new_to_hybrid_map[atom2]

            #check if either permutation is in the keys
            if (atom1_hybrid, atom2_hybrid) in constrained_atoms_dict.keys():
                constraint_from_old_system = constrained_atoms_dict[(atom1_hybrid, atom2_hybrid)]
                if constraint != constraint_from_old_system:
                    raise ValueError("Constraints are changing during switching.")

            if (atom2_hybrid, atom1_hybrid) in constrained_atoms_dict.keys():
                constraint_from_old_system = constrained_atoms_dict[(atom2_hybrid, atom1_hybrid)]
                if constraint != constraint_from_old_system:
                    raise ValueError("Constraints are changing during switching.")

    def _determine_interaction_group(self, atoms_in_interaction):
        """
        This method determines which interaction group the interaction should fall under. There are four groups:

        Those involving unique old atoms: any interaction involving unique old atoms should be completely on at lambda=0
            and completely off at lambda=1

        Those involving unique new atoms: any interaction involving unique new atoms should be completely off at lambda=0
            and completely on at lambda=1

        Those involving core atoms and/or environment atoms: These interactions change their type, and should be the old
            character at lambda=0, and the new character at lambda=1

        Those involving only environment atoms: These interactions are unmodified.

        Parameters
        ----------
        atoms_in_interaction : list of int
            List of (hybrid) indices of the atoms in this interaction

        Returns
        -------
        interaction_group : InteractionGroup enum
            The group to which this interaction should be assigned
        """
        #make the interaction list a set to facilitate operations
        atom_interaction_set = set(atoms_in_interaction)

        #check if the interaction contains unique old atoms
        if len(atom_interaction_set.intersection(self._atom_classes['unique_old_atoms'])) > 0:
            return InteractionGroup.unique_old

        #Do the same for new atoms
        elif len(atom_interaction_set.intersection(self._atom_classes['unique_new_atoms'])) > 0:
            return InteractionGroup.unique_new

        #if the interaction set is a strict subset of the environment atoms, then it is in the environment group
        #and should not be alchemically modified at all.
        elif atom_interaction_set.issubset(self._atom_classes['environment_atoms']):
            return InteractionGroup.environment

        #having covered the cases of all-environment, unique old-containing, and unique-new-containing, anything else
        #should belong to the last class--contains core atoms but not any unique atoms.
        else:
            return InteractionGroup.core

    def _add_bond_force_terms(self):
        """
        This function adds the appropriate bond forces to the system (according to groups defined above). Note that it
        does _not_ add the particles to the force. It only adds the force to facilitate another method adding the
        particles to the force.
        """
        core_energy_expression = '(K/2)*(r-length)^2;'
        core_energy_expression += 'K = (1-lambda_bonds)*K1 + lambda_bonds*K2;' # linearly interpolate spring constant
        core_energy_expression += 'length = (1-lambda_bonds)*length1 + lambda_bonds*length2;' # linearly interpolate bond length

        #create the force and add the relevant parameters
        custom_core_force = openmm.CustomBondForce(core_energy_expression)
        custom_core_force.addGlobalParameter('lambda_bonds', 0.0)
        custom_core_force.addPerBondParameter('length1') # old bond length
        custom_core_force.addPerBondParameter('K1') # old spring constant
        custom_core_force.addPerBondParameter('length2') # new bond length
        custom_core_force.addPerBondParameter('K2') #new spring constant

        self._hybrid_system.addForce(custom_core_force)
        self._hybrid_system_forces['core_bond_force'] = custom_core_force

        #add a bond force for environment and unique atoms (bonds are never scaled for these):
        standard_bond_force = openmm.HarmonicBondForce()
        self._hybrid_system.addForce(standard_bond_force)
        self._hybrid_system_forces['standard_bond_force'] = standard_bond_force

    def _add_angle_force_terms(self):
        """
        This function adds the appropriate angle force terms to the hybrid system. It does not add particles
        or parameters to the force; this is done elsewhere.
        """
        energy_expression  = '(K/2)*(theta-theta0)^2;'
        energy_expression += 'K = (1.0-lambda_angles)*K_1 + lambda_angles*K_2;' # linearly interpolate spring constant
        energy_expression += 'theta0 = (1.0-lambda_angles)*theta0_1 + lambda_angles*theta0_2;' # linearly interpolate equilibrium angle

        #create the force and add relevant parameters
        custom_core_force = openmm.CustomAngleForce(energy_expression)
        custom_core_force.addGlobalParameter('lambda_angles', 0.0)
        custom_core_force.addPerAngleParameter('theta0_1') # molecule1 equilibrium angle
        custom_core_force.addPerAngleParameter('K_1') # molecule1 spring constant
        custom_core_force.addPerAngleParameter('theta0_2') # molecule2 equilibrium angle
        custom_core_force.addPerAngleParameter('K_2') # molecule2 spring constant

        #add the force to the system and the force dict.
        self._hybrid_system.addForce(custom_core_force)
        self._hybrid_system_forces['core_angle_force'] = custom_core_force

        #add an angle term for environment/unique interactions--these are never scaled
        standard_angle_force = openmm.HarmonicAngleForce()
        self._hybrid_system.addForce(standard_angle_force)
        self._hybrid_system_forces['standard_angle_force'] = standard_angle_force

    def _add_torsion_force_terms(self):
        """
        This function adds the appropriate PeriodicTorsionForce terms to the system. Core torsions are interpolated,
        while environment and unique torsions are always on.
        """
        energy_expression  = '(1-lambda_torsions)*U1 + lambda_torsions*U2;'
        energy_expression += 'U1 = K1*(1+cos(periodicity1*theta-phase1));'
        energy_expression += 'U2 = K2*(1+cos(periodicity2*theta-phase2));'

        #create the force and add the relevant parameters
        custom_core_force = openmm.CustomTorsionForce(energy_expression)
        custom_core_force.addGlobalParameter('lambda_torsions', 0.0)
        custom_core_force.addPerTorsionParameter('periodicity1') # molecule1 periodicity
        custom_core_force.addPerTorsionParameter('phase1') # molecule1 phase
        custom_core_force.addPerTorsionParameter('K1') # molecule1 spring constant
        custom_core_force.addPerTorsionParameter('periodicity2') # molecule2 periodicity
        custom_core_force.addPerTorsionParameter('phase2') # molecule2 phase
        custom_core_force.addPerTorsionParameter('K2') # molecule2 spring constant

        #add the force to the system
        self._hybrid_system.addForce(custom_core_force)
        self._hybrid_system_forces['core_torsion_force'] = custom_core_force

        #create and add the torsion term for unique/environment atoms
        standard_torsion_force = openmm.PeriodicTorsionForce()
        self._hybrid_system.addForce(standard_torsion_force)
        self._hybrid_system_forces['standard_torsion_force'] = standard_torsion_force

    def _add_nonbonded_force_terms(self, nonbonded_method):
        """
        Add the nonbonded force terms to the hybrid system. Note that as with the other forces,
        this method does not add any interactions. It only sets up the forces.

        Parameters
        ----------
        nonbonded_method : int
            One of the openmm.NonbondedForce nonbonded methods.
        """
        # Create a CustomNonbondedForce to handle alchemically interpolated nonbonded parameters.
        # Select functional form based on nonbonded method.
        if nonbonded_method in [openmm.NonbondedForce.NoCutoff]:
            sterics_energy_expression, electrostatics_energy_expression = self._nonbonded_custom_nocutoff()
        elif nonbonded_method in [openmm.NonbondedForce.CutoffPeriodic, mm.NonbondedForce.CutoffNonPeriodic]:
            sterics_energy_expression, electrostatics_energy_expression = self._nonbonded_custom_cutoff(force)
        elif nonbonded_method in [openmm.NonbondedForce.PME, mm.NonbondedForce.Ewald]:
            sterics_energy_expression, electrostatics_energy_expression = self._nonbonded_custom_ewald(force)
        else:
            raise Exception("Nonbonded method %s not supported yet." % str(nonbonded_method))
        sterics_energy_expression += self._nonbonded_custom_sterics_common()
        electrostatics_energy_expression += self._nonbonded_custom_electrostatics_common()

        sterics_mixing_rules, electrostatics_mixing_rules = self._nonbonded_custom_mixing_rules()

        # Create CustomNonbondedForce to handle interactions between alchemically-modified atoms and rest of system.
        electrostatics_custom_nonbonded_force = openmm.CustomNonbondedForce("U_electrostatics;" + electrostatics_energy_expression + electrostatics_mixing_rules)
        electrostatics_custom_nonbonded_force.addGlobalParameter("lambda_electrostatics", 0.0);
        electrostatics_custom_nonbonded_force.addPerParticleParameter("chargeA") # partial charge initial
        electrostatics_custom_nonbonded_force.addPerParticleParameter("chargeB") # partial charge final

        self._hybrid_system.addForce(electrostatics_custom_nonbonded_force)
        self._hybrid_system_forces['core_electrostatics_force'] = electrostatics_custom_nonbonded_force

        sterics_custom_nonbonded_force = openmm.CustomNonbondedForce("U_sterics;" + sterics_energy_expression + sterics_mixing_rules)
        sterics_custom_nonbonded_force.addGlobalParameter("lambda_sterics", 0.0);
        sterics_custom_nonbonded_force.addPerParticleParameter("sigmaA") # Lennard-Jones sigma initial
        sterics_custom_nonbonded_force.addPerParticleParameter("epsilonA") # Lennard-Jones epsilon initial
        sterics_custom_nonbonded_force.addPerParticleParameter("sigmaB") # Lennard-Jones sigma final
        sterics_custom_nonbonded_force.addPerParticleParameter("epsilonB") # Lennard-Jones epsilon final

        self._hybrid_system.addForce(sterics_custom_nonbonded_force)
        self._hybrid_system_forces['core_sterics_force'] = sterics_custom_nonbonded_force

        #Add a regular nonbonded force for all interactions that are not changing.
        standard_nonbonded_force = openmm.NonbondedForce()
        self._hybrid_system.addForce(standard_nonbonded_force)
        self._hybrid_system_forces['standard_nonbonded_force'] = standard_nonbonded_force

        #Add a CustomBondForce for exceptions:
        custom_nonbonded_bond_force = self._nonbonded_custom_bond_force(sterics_energy_expression, electrostatics_energy_expression)
        self._hybrid_system.addForce(custom_nonbonded_bond_force)
        self._hybrid_system_forces['core_nonbonded_bond_force'] = custom_nonbonded_bond_force

    def _nonbonded_custom_sterics_common(self):
        """
        Get a custom sterics expression that is common to all nonbonded methods

        Returns
        -------
        sterics_addition : str
            The common softcore sterics energy expression
        """
        sterics_addition = "epsilon = (1-lambda_sterics)*epsilonA + lambda_sterics*epsilonB;" #interpolation
        sterics_addition += "reff_sterics = sigma*((softcore_alpha*lambda_alpha + (r/sigma)^6))^(1/6);" # effective softcore distance for sterics
        sterics_addition += "softcore_alpha = %f;" % self.softcore_alpha
        sterics_addition += "sigma = (1-lambda_sterics)*sigmaA + lambda_sterics*sigmaB;"
        sterics_addition += "lambda_alpha = lambda_sterics*(1-lambda_sterics);"
        return sterics_addition

    def _nonbonded_custom_electrostatics_common(self):
        """
        Get a custom electrostatics expression that is common to all nonbonded methods

        Returns
        -------
        electrostatics_addition : str
            The common electrostatics energy expression
        """
        electrostatics_addition = "chargeprod = (1-lambda_electrostatics)*chargeprodA + lambda_electrostatics*chargeprodB;" #interpolation
        electrostatics_addition += "reff_electrostatics = sqrt(softcore_beta*lambda_beta + r^2);" # effective softcore distance for electrostatics
        electrostatics_addition += "softcore_beta = %f;" % (self.softcore_beta / self.softcore_beta.in_unit_system(unit.md_unit_system).unit)
        electrostatics_addition += "ONE_4PI_EPS0 = %f;" % ONE_4PI_EPS0 # already in OpenMM units
        electrostatics_addition += "lambda_beta = lambda_electrostatics*(1-lambda_electrostatics);"
        return electrostatics_addition

    def _nonbonded_custom_nocutoff(self):
        """
        Get a part of the nonbonded energy expression when there is no cutoff.

        Returns
        -------
        sterics_energy_expression : str
            The energy expression for U_sterics
        electrostatics_energy_expression : str
            The energy expression for electrostatics
        """
        # soft-core Lennard-Jones
        sterics_energy_expression = "U_sterics = 4*epsilon*x*(x-1.0); x = (sigma/reff_sterics)^6;"
        # soft-core Coulomb
        electrostatics_energy_expression = "U_electrostatics = ONE_4PI_EPS0*chargeprod/reff_electrostatics;"
        return sterics_energy_expression, electrostatics_energy_expression

    def _nonbonded_custom_cutoff(self, epsilon_solvent, r_cutoff):
        """
        Get the energy expressions for sterics and electrostatics under a reaction field assumption.

        Parameters
        ----------
        epsilon_solvent : float
            The reaction field dielectric
        r_cutoff : float
            The cutoff distance

        Returns
        -------
        sterics_energy_expression : str
            The energy expression for U_sterics
        electrostatics_energy_expression : str
            The energy expression for electrostatics
        """
        # soft-core Lennard-Jones
        sterics_energy_expression = "U_sterics = 4*epsilon*x*(x-1.0); x = (sigma/reff_sterics)^6;"

        electrostatics_energy_expression = "U_electrostatics = ONE_4PI_EPS0*chargeprod*(reff_electrostatics^(-1) + k_rf*reff_electrostatics^2 - c_rf);"
        k_rf = r_cutoff**(-3) * ((epsilon_solvent - 1) / (2*epsilon_solvent + 1))
        c_rf = r_cutoff**(-1) * ((3*epsilon_solvent) / (2*epsilon_solvent + 1))
        electrostatics_energy_expression += "k_rf = %f;" % (k_rf / k_rf.in_unit_system(unit.md_unit_system).unit)
        electrostatics_energy_expression += "c_rf = %f;" % (c_rf / c_rf.in_unit_system(unit.md_unit_system).unit)
        return sterics_energy_expression, electrostatics_energy_expression

    def _nonbonded_custom_ewald(self, alpha_ewald, delta, r_cutoff):
        """
        Get the energy expression for Ewald treatment.

        Parameters
        ----------
        alpha_ewald : float
            The Ewald alpha parameter
        delta : float
            The PME error tolerance
        r_cutoff : float
            The cutoff distance

        Returns
        -------
        sterics_energy_expression : str
            The energy expression for U_sterics
        electrostatics_energy_expression : str
            The energy expression for electrostatics
        """
        # soft-core Lennard-Jones
        sterics_energy_expression = "U_sterics = 4*epsilon*x*(x-1.0); x = (sigma/reff_sterics)^6;"
        if alpha_ewald == 0.0:
            # If alpha is 0.0, alpha_ewald is computed by OpenMM from from the error tolerance.
            alpha_ewald = np.sqrt(-np.log(2*delta)) / r_cutoff
        electrostatics_energy_expression = "U_electrostatics = ONE_4PI_EPS0*chargeprod*erfc(alpha_ewald*reff_electrostatics)/reff_electrostatics;"
        electrostatics_energy_expression += "alpha_ewald = %f;" % (alpha_ewald / alpha_ewald.in_unit_system(unit.md_unit_system).unit)
        return sterics_energy_expression, electrostatics_energy_expression

    def _nonbonded_custom_mixing_rules(self):
        """
        Mixing rules for the custom nonbonded force.

        Returns
        -------
        sterics_mixing_rules : str
            The mixing expression for sterics
        electrostatics_mixing_rules : str
            The mixiing rules for electrostatics
        """
        # Define mixing rules.
        sterics_mixing_rules = "epsilonA = sqrt(epsilonA1*epsilonA2);" # mixing rule for epsilon
        sterics_mixing_rules += "epsilonB = sqrt(epsilonB1*epsilonB2);" # mixing rule for epsilon
        sterics_mixing_rules += "sigmaA = 0.5*(sigmaA1 + sigmaA2);" # mixing rule for sigma
        sterics_mixing_rules += "sigmaB = 0.5*(sigmaB1 + sigmaB2);" # mixing rule for sigma
        electrostatics_mixing_rules = "chargeprodA = chargeA1*chargeA2;" # mixing rule for charges
        electrostatics_mixing_rules += "chargeprodB = chargeB1*chargeB2;" # mixing rule for charges
        return sterics_mixing_rules, electrostatics_mixing_rules

    def _nonbonded_custom_bond_force(self, sterics_energy_expression, electrostatics_energy_expression):
        """
        Add a CustomBondForce to represent the exceptions in the NonbondedForce

        Parameters
        ----------
        sterics_energy_expression : str
            The complete energy expression being used for sterics
        electrostatics_energy_expression : str
            The complete energy expression being used for electrostatics

        Returns
        -------
        custom_bond_force : openmm.CustomBondForce
            The custom bond force for the nonbonded exceptions
        """
        #Create the force and add its relevant parameters.
        custom_bond_force = openmm.CustomBondForce("U_sterics + U_electrostatics;" + sterics_energy_expression + electrostatics_energy_expression)
        custom_bond_force.addGlobalParameter("lambda_electrostatics", 0.0)
        custom_bond_force.addGlobalParameter("lambda_sterics", 0.0)
        custom_bond_force.addPerBondParameter("chargeprodA")
        custom_bond_force.addPerBondParameter("sigmaA")
        custom_bond_force.addPerBondParameter("epsilonA")
        custom_bond_force.addPerBondParameter("chargeprodB")
        custom_bond_force.addPerBondParameter("sigmaB")
        custom_bond_force.addPerBondParameter("epsilonB")

        return custom_bond_force

    def _find_bond_parameters(self, bond_force, index1, index2):
        """
        This is a convenience function to find bond parameters in another system given the two indices.

        Parameters
        ----------
        bond_force : openmm.HarmonicBondForce
            The bond force where the parameters should be found
        index1 : int
           Index1 (order does not matter) of the bond atoms
        index2 : int
           Index2 (order does not matter) of the bond atoms

        Returns
        -------
        bond_parameters : list
            List of relevant bond parameters
        """
        index_set = {index1, index2}
        #loop through all the bonds:
        for bond_index in range(bond_force.getNumBonds()):
            [index1_term, index2_term, r0, k] = bond_force.getBondParameters(bond_index)
            if index_set=={index1_term, index2_term}:
                return [index1_term, index2_term, r0, k]

        raise ValueError("The requested bond was not found.")

    def handle_harmonic_bonds(self):
        """
        This method adds the appropriate interaction for all bonds in the hybrid system. The scheme used is:

        1) If the two atoms are both in the core, then we add to the CustomBondForce and interpolate between the two
            parameters
        2) Otherwise, we add the bond to a regular bond force.
        """
        old_system_bond_force = self._old_system_forces['HarmonicBondForce']
        new_system_bond_force = self._new_system_forces['HarmonicBondForce']

        #first, loop through the old system bond forces and add relevant terms
        for bond_index in range(old_system_bond_force.getNumBonds()):
            #get each set of bond parameters
            [index1_old, index2_old, r0_old, k_old] = old_system_bond_force.getBondParameters(bond_index)

            #map the indices to the hybrid system, for which our atom classes are defined.
            index1_hybrid = self._old_to_hybrid_map[index1_old]
            index2_hybrid = self._old_to_hybrid_map[index2_old]
            index_set = {index1_hybrid, index2_hybrid}

            #now check if it is a subset of the core atoms (that is, both atoms are in the core)
            #if it is, we need to find the parameters in the old system so that we can interpolate
            if index_set.issubset(self._atom_classes['core_atoms']):
                index1_new = self._topology_proposal.old_to_new_atom_map[index1_old]
                index2_new = self._topology_proposal.old_to_new_atom_map[index2_old]
                [index1, index2, r0_new, k_new] = self._find_bond_parameters(new_system_bond_force, index1_new, index2_new)
                self._hybrid_system_forces['core_bond_force'].addBond([index1_hybrid, index2_hybrid,[r0_old, k_old, r0_new, k_new]])

            #otherwise, we just add the same parameters as those in the old system.
            else:
                self._hybrid_system_forces['standard_bond_force'].addBond([index1_hybrid, index2_hybrid, r0_old, k_old])