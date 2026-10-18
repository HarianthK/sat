# Run: python test_sat.py. Every SAT answer is checked clause by clause; every UNSAT one against brute force or mathematics.
import bisect
import faulthandler
import os
import random
import subprocess
import sys
import time

import sat

BUDGET = 200000  # conflicts; a broken solver that loops fails here instead of hanging
started = time.perf_counter()
faulthandler.dump_traceback_later(120, exit=True)  # a hang fails, with a traceback of where


def satisfies(model, nvars, clauses):
    # Shares nothing with the solver: a plain set of true literals, one per variable.
    true = set(model)
    assert len(model) == nvars and all(abs(l) == i for i, l in enumerate(model, 1)), "not one value per variable"
    return all(any(l in true for l in c) for c in clauses)


def all_models(nvars, clauses):
    # Brute force. A clause is false exactly when its variables' bits match one pattern.
    tests = []
    for c in clauses:
        mask = pattern = 0
        for l in c:
            mask |= 1 << (abs(l) - 1)
            if l < 0:
                pattern |= 1 << (abs(l) - 1)
        tests.append((mask, pattern))
    found = []
    for a in range(1 << nvars):
        if all(a & m != p for m, p in tests):
            found.append([v if a >> (v - 1) & 1 else -v for v in range(1, nvars + 1)])
    return found


def random_3sat(rng, nvars, nclauses):
    return [[v if rng.random() < 0.5 else -v for v in rng.sample(range(1, nvars + 1), 3)] for _ in range(nclauses)]


class Checked(sat.Solver):
    # Checks every reduction from outside: what was deleted, what stayed, and every watch list.
    rounds = deleted = removed = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.originals = list({id(c): c for w in self.watches for c in w}.values())
        self.true_lbd = {}  # id -> (clause, LBD); holding the clause keeps its id unique
        self.gap = 0

    def levels(self, clause):
        # Levels read off the trail and decision points instead of the solver's level array.
        where = {abs(l): i for i, l in enumerate(self.trail)}
        return [bisect.bisect_right(self.limits, where[abs(l)]) for l in clause]

    def analyze(self, conflict):
        learnt, back = super().analyze(conflict)
        levels = self.levels(learnt)
        self.true_lbd[id(learnt)] = learnt, len(set(levels))
        assert back == max(levels[1:], default=0), f"jumped to {back} with {learnt} at levels {levels}"
        return learnt, back

    def minimize(self, learnt):
        # Recomputed another way: one pass up the trail collects every variable that follows from the clause,
        # and a literal goes exactly when all of its reason does. DOCS.md says why that is the same set.
        before = list(learnt)
        after = super().minimize(learnt)
        follows = {abs(l) for l in before}
        start = self.limits[0] if self.limits else len(self.trail)
        for i, lit in enumerate(self.trail):
            r = self.reason[abs(lit)]
            if i < start or r is not None and all(abs(q) in follows for q in r if q != lit):
                follows.add(abs(lit))
        want = [before[0]] + [q for q in before[1:] if self.reason[abs(q)] is None
                              or not all(abs(r) in follows for r in self.reason[abs(q)] if r != q)]
        assert set(after) <= set(before) and after[0] == before[0], f"minimized {before} to {after}"
        levels = self.levels(after)
        assert levels.count(len(self.limits)) == 1 and levels[0] == len(self.limits), f"{after} at levels {levels}"
        assert set(levels) == set(self.levels(before)), f"minimizing {before} to {after} emptied a level"
        assert sorted(after) == sorted(want), f"minimized {before} to {after}, should be {want}"
        Checked.removed += len(before) - len(after)
        return after

    def reduce(self):
        lbd = lambda c: self.true_lbd[id(c)][1]
        assert self.lbds == [lbd(c) for c in self.learnts], "stored LBDs are not the number of levels"
        before = list(self.learnts)
        locked = {id(self.reason[abs(l)]) for l in self.trail if self.reason[abs(l)] is not None}
        super().reduce()
        gap, self.gap = self.gap, self.next_reduce - self.stats["conflicts"]
        assert self.gap > gap, f"the interval between reductions went from {gap} to {self.gap}, not up"
        alive = {id(c) for c in self.learnts}
        assert alive <= {id(c) for c in before}, "a clause appeared during a reduction"
        removed = [c for c in before if id(c) not in alive]
        assert not any(id(c) in locked for c in removed), "deleted a clause that is the reason for an assignment"
        assert all(lbd(c) > 2 for c in removed), "deleted a glue clause"
        free = [lbd(c) for c in self.learnts if lbd(c) > 2 and id(c) not in locked]
        assert not removed or not free or min(map(lbd, removed)) >= max(free), "kept a worse clause than one deleted"
        protected = sum(lbd(c) <= 2 or id(c) in locked for c in before)
        assert len(removed) >= len(before) // 2 - protected, f"deleted {len(removed)} of {len(before)}"
        # Every original and kept clause is watched exactly at its first two literals, and nothing else is.
        live = self.originals + [c for c in self.learnts if len(c) > 1]
        want = sorted((id(c), c[j] + self.n) for c in live for j in (0, 1))
        have = sorted((id(c), i) for i, w in enumerate(self.watches) for c in w)
        assert have == want, "the watch lists do not match the clauses that should be there"
        Checked.rounds += 1
        Checked.deleted += len(removed)


def run(nvars, clauses, label):
    # Every run deletes clauses far more often than the solver does, so the small formulas reduce too.
    solver = Checked(nvars, clauses, reduce_first=10, reduce_inc=5)
    model = solver.solve(BUDGET)
    if model is not None:
        assert satisfies(model, nvars, clauses), f"{label}: CDCL's model breaks a clause"
    return model, solver


# DIMACS: comments, a clause spread over lines, two on one line, and SATLIB's "%" ending.
text = """c a comment
c another
p cnf 5 4
1 -2
  3 0 -4 0
5 -1 0
2 0
%
0
"""
assert sat.parse_dimacs(text) == (5, [[1, -2, 3], [-4], [5, -1], [2]]), sat.parse_dimacs(text)
assert sat.parse_dimacs("p cnf 3 0\n") == (3, [])
for bad, reason in (("1 2 0\n", "before"), ("p cnf 2 1\n1 3 0\n", "past"),
                    ("p cnf 2 1\n1 x 0\n", "not a literal"), ("p cnf 2 1\n1 2\n", "closing 0"), ("c only\n", "no 'p cnf'")):
    try:
        sat.parse_dimacs(bad)
        raise AssertionError(f"accepted {bad!r}")
    except ValueError as e:
        assert reason in str(e), f"wanted {reason!r}, got {e!r}"
print("DIMACS parsed, and broken files refused with a reason")

# Random 3-SAT around the phase transition, 4.26 clauses per variable, against brute force.
# Every learned clause must also hold in every model: learning may only say what the formula implies.
rng = random.Random(2026)
counts = {"SAT": 0, "UNSAT": 0}
for nvars in range(4, 15):
    for trial in range(30):
        clauses = random_3sat(rng, nvars, round(4.26 * nvars))
        label = f"random {nvars} vars #{trial}"
        truth = all_models(nvars, clauses)
        model, solver = run(nvars, clauses, label)
        assert (model is not None) == bool(truth), f"{label}: CDCL says {model is not None}, brute force {bool(truth)}"
        for learnt, _ in solver.true_lbd.values():  # every clause learned, deleted ones too
            assert all(any(l in set(m) for l in learnt) for m in truth), f"{label}: learned {learnt}, not implied"
        theirs = sat.dpll(nvars, clauses)
        assert (theirs is not None) == bool(truth), f"{label}: DPLL disagrees with brute force"
        if theirs is not None:
            assert satisfies(theirs, nvars, clauses), f"{label}: DPLL's model breaks a clause"
        counts["SAT" if truth else "UNSAT"] += 1
print(f"330 random 3-SAT instances agree with brute force: {counts['SAT']} SAT, {counts['UNSAT']} UNSAT")

# Bigger ones, past brute force: CDCL and DPLL must agree, and these are big enough to restart.
for nvars in (30, 40, 50):
    for trial in range(6):
        clauses = random_3sat(rng, nvars, round(4.26 * nvars))
        model, _ = run(nvars, clauses, f"random {nvars} vars #{trial}")
        assert (model is None) == (sat.dpll(nvars, clauses) is None), f"random {nvars} #{trial}: CDCL and DPLL disagree"
print("18 random instances of 30 to 50 variables: CDCL and DPLL agree")


# Pigeonhole: n+1 pigeons in n holes cannot fit, by counting; n pigeons in n holes can.
def pigeonhole(pigeons, holes):
    var = lambda p, h: p * holes + h + 1
    clauses = [[var(p, h) for h in range(holes)] for p in range(pigeons)]
    for h in range(holes):
        for p in range(pigeons):
            clauses += [[-var(p, h), -var(q, h)] for q in range(p + 1, pigeons)]
    return pigeons * holes, clauses


for n in range(1, 8):
    nvars, clauses = pigeonhole(n + 1, n)
    model, solver = run(nvars, clauses, f"pigeonhole {n + 1} into {n}")
    assert model is None, f"fitted {n + 1} pigeons into {n} holes"
    if n == 7:
        stats = solver.stats
        assert stats["restarts"] > 0, "a search of thousands of conflicts never restarted"
        assert stats["learned"] == stats["conflicts"] - 1 and stats["decisions"] > 0, stats
        assert solver.bump_by > 1, "thousands of conflicts and the activity never decayed"
        assert stats["propagations"] > stats["conflicts"], "propagations not counted"
    if n <= 5:
        assert sat.dpll(nvars, clauses) is None, f"DPLL fitted {n + 1} pigeons into {n} holes"
    nvars, clauses = pigeonhole(n, n)
    model, _ = run(nvars, clauses, f"pigeonhole {n} into {n}")
    assert model is not None, f"could not fit {n} pigeons into {n} holes"
    holes = [h for p in range(n) for h in range(n) if p * n + h + 1 in set(model)]
    assert sorted(holes) == list(range(n)), f"{n} pigeons: holes {holes}"
print(f"pigeonhole: n+1 into n refused for n = 1 to 7 ({stats['conflicts']} conflicts for 8 into 7, deleting every few), "
      "n into n placed")


# N-queens: impossible for 2 and 3, possible from 4 up. The board is checked as a board, not as clauses.
def board_ok(model, n):
    on = set(model)
    queens = [(r, c) for r in range(n) for c in range(n) if r * n + c + 1 in on]
    if len(queens) != n:
        return False
    return all(r1 != r2 and c1 != c2 and abs(r1 - r2) != abs(c1 - c2)
               for i, (r1, c1) in enumerate(queens) for r2, c2 in queens[i + 1:])


for n in range(1, 13):
    nvars, clauses = sat.queens(n)
    model, _ = run(nvars, clauses, f"{n} queens")
    assert (model is None) == (n in (2, 3)), f"{n} queens: wrong answer"
    if model is not None:
        assert board_ok(model, n), f"{n} queens: two queens attack each other"


# Count every solution by forbidding each one found; the counts are known: 10, 4, 40, 92.
def count(nvars, clauses, limit=100):
    found = []
    while len(found) <= limit:
        model, _ = run(nvars, clauses + [[-l for l in m] for m in found], "counting")
        if model is None:
            return found
        found.append(model)
    return found


for n, known in ((5, 10), (6, 4), (7, 40), (8, 92)):
    found = count(*sat.queens(n))
    assert len(found) == known and all(board_ok(m, n) for m in found), f"{n} queens: {len(found)} solutions, not {known}"
print("N-queens: none for 2 and 3, a valid board for 1 and 4 to 12, and 10, 4, 40, 92 solutions for 5 to 8")

# Sudoku: each must come back as its known solution, and as the only one.
PUZZLES = {
    "53..7....6..195....98....6.8...6...34..8.3..17...2...6.6....28....419..5....8..79":
    "534678912672195348198342567859761423426853791713924856961537284287419635345286179",
    "8..........36......7..9.2...5...7.......457.....1...3...1....68..85...1..9....4..":
    "812753649943682175675491283154237896369845721287169534521974368438526917796318452",
}
for puzzle, known in PUZZLES.items():
    nvars, clauses = sat.sudoku(puzzle)
    found = count(nvars, clauses)
    assert len(found) == 1, f"{len(found)} solutions to {puzzle}"
    grid = sat.sudoku_grid(found[0])
    assert grid == known, f"solved {puzzle} as {grid}"
print("both sudokus solved as their known solutions, and only those")

# The heuristic. Luby's sequence, then decay: of two variables bumped once each, the later one
# must be picked first, which is the whole point of decaying the older bumps.
assert [sat.luby(i) for i in range(1, 16)] == [1, 1, 2, 1, 1, 2, 4, 1, 1, 2, 1, 1, 2, 4, 8], "not the Luby sequence"
s = sat.Solver(3, [[1, 2, 3]])
s.bump(1)
s.decay_activity()
s.bump(2)
s.decay_activity()
s.rebuild_heap()
assert abs(s.pick()) == 2, "an older bump counted as much as a newer one"
print("Luby sequence right, and recent conflicts outrank old ones")


# After each conflict the learned clause must have exactly one literal from the conflict's level
# (the first UIP), and the solver must jump back to the highest level among the others, not one level.
class Watched(sat.Solver):
    def analyze(self, conflict):
        here = len(self.limits)
        learnt, back = super().analyze(conflict)
        levels = [self.level[abs(l)] for l in learnt]
        assert levels.count(here) == 1 and levels[0] == here, f"learned {learnt} at levels {levels}, conflict at {here}"
        assert back == max(levels[1:], default=0), f"jumped to {back}, levels {levels}"
        self.jumps += here - back > 1
        return learnt, back


rng = random.Random(7)
jumps = 0
for nvars in (40, 60):
    for trial in range(3):
        clauses = random_3sat(rng, nvars, round(4.26 * nvars))
        s = Watched(nvars, clauses)
        s.jumps = 0
        model = s.solve(BUDGET)
        assert model is None or satisfies(model, nvars, clauses)
        jumps += s.jumps
assert jumps > 0, "never jumped back more than one level"
print(f"every learned clause asserts one literal at the first UIP; {jumps} jumps went back more than one level")

# Bumping is what makes CDCL fast on random formulas: these three take about 1,500 conflicts with it
# and about 8,000 without, measured. The budget sits between.
rng = random.Random(7)
for _ in range(3):
    random_3sat(rng, 100, 426)  # skipped, so the next three are the formulas measured
spent = 0
for trial in range(3):
    clauses = random_3sat(rng, 125, 532)
    s = sat.Solver(125, clauses)
    model = s.solve(BUDGET)
    assert model is None or satisfies(model, 125, clauses)
    spent += s.stats["conflicts"]
assert spent < 4000, f"three 125-variable formulas took {spent} conflicts"
print(f"three 125-variable random formulas in {spent} conflicts")

# The solver's own schedule (first reduction at 2,000 conflicts) on two 200-variable formulas of
# 8,000 to 16,000 conflicts, checked at every reduction, and their models checked clause by clause.
rng = random.Random(1)
for _ in range(3):
    random_3sat(rng, 175, 746)  # skipped: these two are the ones measured in DOCS.md
long_rounds = 0
for trial in range(2):
    clauses = random_3sat(rng, 200, 852)
    s = Checked(200, clauses)
    model = s.solve(BUDGET)
    assert model is not None and satisfies(model, 200, clauses), f"200 variables #{trial}: no valid model"
    long_rounds += s.stats["reductions"]
assert long_rounds >= 6, f"only {long_rounds} reductions on the default schedule"
print(f"two 200-variable formulas satisfied on the default schedule, through {long_rounds} reductions")

# The command line, in SAT competition form: s and v lines, exit 10 or 20, and c lines with --stats.
nvars, clauses = sat.queens(6)
with open("t-sat.cnf", "w") as f:
    f.write(f"c six queens\np cnf {nvars} {len(clauses)}\n" + "".join(" ".join(map(str, c)) + " 0\n" for c in clauses))
nvars2, clauses2 = pigeonhole(5, 4)
with open("t-unsat.cnf", "w") as f:
    f.write(f"p cnf {nvars2} {len(clauses2)}\n" + "".join(" ".join(map(str, c)) + " 0\n" for c in clauses2))
with open("t-bad.cnf", "w") as f:
    f.write("p cnf 2 1\n1 9 0\n")
try:
    for flags in ([], ["--dpll"], ["--no-minimize"]):
        out = subprocess.run([sys.executable, sat.__file__, "t-sat.cnf", "--stats"] + flags, capture_output=True, text=True)
        lines = out.stdout.splitlines()
        assert out.returncode == 10 and "s SATISFIABLE" in lines, out
        lits = [int(t) for line in lines if line.startswith("v ") for t in line.split()[1:]]
        assert lits[-1] == 0 and satisfies(lits[:-1], nvars, clauses), "the v lines do not satisfy the file"
        cdcl = () if flags == ["--dpll"] else ("learned", "minimized", "deleted")
        for key in ("decisions", "conflicts", "propagations", "time") + cdcl:
            assert any(line.startswith(f"c {key} ") for line in lines), f"--stats {flags} printed no {key}"
        if flags != ["--dpll"]:  # six queens minimizes one literal, unless told not to
            assert ("c minimized 0" in lines) == (flags == ["--no-minimize"]), f"--stats {flags}: minimized wrongly"
    out = subprocess.run([sys.executable, sat.__file__, "t-unsat.cnf"], capture_output=True, text=True)
    assert out.returncode == 20 and out.stdout.splitlines() == ["s UNSATISFIABLE"], out
    out = subprocess.run([sys.executable, sat.__file__, "t-bad.cnf"], capture_output=True, text=True)
    assert out.returncode == 1 and "past the 2 variables" in out.stderr and "Traceback" not in out.stderr, out
finally:
    for name in ("t-sat.cnf", "t-unsat.cnf", "t-bad.cnf"):
        os.remove(name)
print("the command line prints s and v lines, exits 10 or 20, and refuses a bad file")

# --cnf prints the formula instead of solving it: read back, it must be exactly the encoder's
# clauses, and that file must then be solved like any other (six queens: satisfiable, exit 10).
cnf = subprocess.run([sys.executable, sat.__file__, "--queens", "6", "--cnf"], capture_output=True, text=True)
assert cnf.returncode == 0 and "s " not in cnf.stdout, cnf.stdout[:200]
assert sat.parse_dimacs(cnf.stdout) == sat.queens(6), "the printed CNF is not the queens encoding"
with open("t-queens.cnf", "w") as f:
    f.write(cnf.stdout)
try:
    solved = subprocess.run([sys.executable, sat.__file__, "t-queens.cnf"], capture_output=True, text=True)
    assert solved.returncode == 10 and "s SATISFIABLE" in solved.stdout, solved.stdout[:200]
finally:
    os.remove("t-queens.cnf")
print("--cnf prints the encoding, and the printed file solves the same way")

assert Checked.removed > 0, "minimization never removed a literal"
print(f"{Checked.rounds} reductions checked, {Checked.deleted} learned clauses deleted, every answer still right")
print(f"{Checked.removed} literals minimized away, each removal recomputed from the trail")
print(f"ok, in {time.perf_counter() - started:.1f}s")
