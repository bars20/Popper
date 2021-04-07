import os
import re
import clingo
import numbers
from . import core
from collections import OrderedDict
from ortools.sat.python import cp_model

# -----------------------------------------------------------------------------
NUM_OF_LITERALS = (
"""
%%% External atom for number of literals in the program %%%%%
#external size_in_literals(n).
:-
    size_in_literals(n),
    #sum{K+1,Clause : clause_size(Clause,K)} != n.
""")

NUM_OF_CLAUSES = (
"""
%%% External atom for number of clauses in the program %%%%%
#external size_in_clauses(n).
:-
    size_in_clauses(n),
    #sum{K : clause(K)} != n.
""")

def arg_to_symbol(arg):
    if isinstance(arg, core.Variable):
        assert False, 'Grounding only symbols for now'
    if isinstance(arg, tuple):
        return clingo.Tuple_(tuple(arg_to_symbol(a) for a in arg))
    if isinstance(arg, numbers.Number):
        return clingo.Number(arg)
    if isinstance(arg, str):
        return clingo.Function(arg)
    assert False, 'Unhandled type'

def atom_to_symbol(lit):
    args = tuple(arg_to_symbol(arg) for arg in lit.arguments)
    return clingo.Function(name = lit.predicate.name, arguments = args)

class Clingo():
    def __init__(self, kbpath):
        self.solver = clingo.Control([])
        self.max_vars = 0
        self.max_clauses = 0
        
        # Runtime attributes
        self.grounded = []
        self.assigned = OrderedDict()
        self.added = OrderedDict()

        self.load_basic(kbpath)

    def load_basic(self, kbpath):
        # Load Alan.
        alan_path = os.path.abspath('popper/alan/')
        prevwd = os.getcwd()
        with open(alan_path + '/alan.pl') as alan:
            os.chdir(alan_path)
            self.solver.add('alan', [], alan.read())
            os.chdir(prevwd)

        # Load Mode file
        with open(kbpath + 'modes.pl') as modefile:
            contents = modefile.read()
            self.max_vars = int(re.search("max_vars\((\d+)\)\.", contents).group(1))
            self.max_clauses = int(re.search("max_clauses\((\d+)\)\.", contents).group(1)) 
            self.solver.add('modes_file', [], contents)

        # Reset number of literals and clauses because size_in_literals literal
        # within Clingo is reset by loading Alan? (bottom two).
        self.solver.add('invented', ['predicate', 'arity'], 
                        '#external invented(pred,arity).')
        self.solver.add('number_of_literals', ['n'], NUM_OF_LITERALS)
        self.solver.add('number_of_clauses', ['n'], NUM_OF_CLAUSES)

        # Ground 'alan' and 'modes_file'
        parts = [('alan', []), ('modes_file', [])]
        self.solver.ground(parts)
        self.grounded.extend(parts)
    
    def get_model(self):
        with self.solver.solve(yield_ = True) as handle:
            m = handle.model()
            if m:
                return m.symbols(atoms = True)
            return m
    
    def update_number_of_literals(self, size):
        # 1. Release those that have already been assigned
        for atom, truthiness in self.assigned.items():
            if atom.predicate.name == 'size_in_literals' and truthiness:
                self.assigned[atom] = False
                atom_args = [clingo.Number(arg) for arg in atom.arguments]
                symbol = clingo.Function('size_in_literals', atom_args)
                self.solver.release_external(symbol)
        
        # 2. Ground the new size
        self.solver.ground([('number_of_literals', [clingo.Number(size)])])
        
        # 3. Assign the new size
        atom = core.Atom('size_in_literals', (size,))
        self.assigned[atom] = True

        # @NOTE: Everything passed to Clingo must be Symbol. Refactor after 
        # Clingo updates their cffi API
        symbol = clingo.Function('size_in_literals', [clingo.Number(size)])
        self.solver.assign_external(symbol, True)

    def add(self, code, name = None, arguments = [], base = False):
        if name is None:
            name = f'fragment{len(self.added) + 1}'
        
        if base:
            self.solver.add(name, arguments, code)
        self.added[name] = code

    def ground(self, *args, context = None):
        parts = [(name, []) for name in args]
        for name, args in parts:
            # @NOTE: Not needed in this implementation
            assert len(args) == 0, 'Arguments not yet supported'

            ast = self.added[name]
            model = cp_model.CpModel()

            vars_to_cp = {}
            cp_to_vars = {}

            var_vals = []
            for var in ast.all_vars():
                if isinstance(var, core.ClauseVariable):
                    cp_var = model.NewIntVar(0, self.max_clauses - 1, var.name)
                elif isinstance(var, core.VarVariable):
                    cp_var = model.NewIntVar(0, self.max_vars - 1, var.name)
                    var_vals.append(cp_var)
                else:
                    assert False, 'Whut??' # Jk: Hahahaha 
                vars_to_cp[var] = cp_var
                cp_to_vars[cp_var] = var
            
            for lit in ast.body:
                if not isinstance(lit, core.ConstraintLiteral):
                    continue
                def args():
                    for arg in lit.arguments:
                        if isinstance(arg, core.Variable):
                            yield vars_to_cp[arg]
                        else:
                            yield arg
                x = lit.predicate.operator(*args())
                model.Add(x)
            
            class SolutionPrinter(cp_model.CpSolverSolutionCallback):
                
                def __init__(self, cp_to_vars):
                    cp_model.CpSolverSolutionCallback.__init__(self)
                    self.cp_to_vars = cp_to_vars
                    self.assignments = []

                def on_solution_callback(self):
                    assignment = dict((self.cp_to_vars[k], self.Value(k))
                                      for k in self.cp_to_vars.keys())
                    self.assignments.append(assignment)

            cp_solver = cp_model.CpSolver()
            solution_printer = SolutionPrinter(cp_to_vars)
            status = cp_solver.SearchForAllSolutions(model, solution_printer)
            clbody = tuple(lit for lit in ast.body
                           if not isinstance(lit, core.ConstraintLiteral))
            clause = core.Clause(head = ast.head, body = clbody)

            with self.solver.backend() as backend:
                for assignment in solution_printer.assignments:
                    ground_clause = clause.ground(assignment)
                    
                    head_symbs = []
                    head_atoms = []
                    body_symbs = []
                    body_lits = []
                    for lit in ground_clause.head:
                        symbol = atom_to_symbol(lit)
                        head_symbs.append(symbol)
                        head_atoms.append(backend.add_atom(symbol))
                    for lit in ground_clause.body:
                        symbol = atom_to_symbol(lit)
                        body_symbs.append(symbol)
                        body_atom = backend.add_atom(symbol)
                        body_lits.append(body_atom if lit.polarity else -body_atom)
                    
                    backend.add_rule(head_atoms, body_lits, choice = False)
        
        self.grounded.extend(parts)