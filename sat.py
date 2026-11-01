# A CDCL SAT solver: watched literals, first-UIP learning and minimization, backjumping, VSIDS, Luby restarts, LBD deletion.
# Run: python sat.py FILE.cnf [--stats] [--dpll] [--keep] [--no-minimize] [--elim] [--proof OUT] [--check-proof PROOF], or --queens N or --sudoku 81-CHARS
import argparse
import heapq
import itertools
import sys
import time


def parse_dimacs(text):
    nvars, clauses, clause = None, [], []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if line.startswith("%"):
            break  # SATLIB's uf files end with a "%" line and a stray 0
        if not line or line.startswith("c"):
            continue
        if line.startswith("p"):
            parts = line.split()
            if len(parts) != 4 or parts[1] != "cnf" or not parts[2].isdigit():
                raise ValueError(f"line {number}: not a 'p cnf VARS CLAUSES' line: {line!r}")
            nvars = int(parts[2])
            continue
        if nvars is None:
            raise ValueError(f"line {number}: a clause before the 'p cnf' line")
        for token in line.split():
            try:
                lit = int(token)
            except ValueError:
                raise ValueError(f"line {number}: {token!r} is not a literal") from None
            if lit == 0:
                clauses.append(clause)
                clause = []
            elif abs(lit) > nvars:
                raise ValueError(f"line {number}: literal {lit} is past the {nvars} variables declared")
            else:
                clause.append(lit)
    if nvars is None:
        raise ValueError("no 'p cnf' line")
    if clause:
        raise ValueError("the last clause has no closing 0")
    return nvars, clauses


def luby(i):
    k = i.bit_length()
    if i == (1 << k) - 1:
        return 1 << (k - 1)
    return luby(i - (1 << (k - 1)) + 1)


class Solver:
    # Literals are signed ints; per-literal arrays are indexed by literal + n.
    def __init__(self, nvars, clauses, decay=0.95, restart_unit=100, reduce_first=2000, reduce_inc=300,
                 minimize=True, proof=None):
        n = self.n = nvars
        self.proof = proof  # a list that gets one DRAT line per learned or deleted clause
        self.val = [0] * (2 * n + 1)  # 1 true, -1 false, 0 unassigned
        self.watches = [[] for _ in range(2 * n + 1)]  # clauses watching each literal
        self.level = [0] * (n + 1)
        self.reason = [None] * (n + 1)
        self.seen = [False] * (n + 1)
        self.trail, self.limits, self.qhead = [], [], 0
        self.activity = [0.0] * (n + 1)
        self.bump_by, self.decay, self.restart_unit = 1.0, decay, restart_unit
        self.phase = [False] * (n + 1)
        self.heap = [(0.0, v) for v in range(1, n + 1)]
        self.learnts, self.lbds = [], []  # lbds[i] is the LBD of learnts[i]
        self.reduce_every, self.reduce_inc = reduce_first, reduce_inc  # reduce_first=None keeps every clause
        self.next_reduce = reduce_first
        self.minimizing = minimize
        self.stats = dict(decisions=0, conflicts=0, propagations=0, learned=0, minimized=0, restarts=0,
                          reductions=0, deleted=0)
        self.ok = True
        for c in clauses:
            self.add(c)

    def add(self, lits):
        c = list(dict.fromkeys(lits))
        if any(-l in c for l in c):
            return  # always true
        if not c:
            self.ok = False
        elif len(c) == 1:
            if self.val[c[0] + self.n] == -1:
                self.ok = False
            elif self.val[c[0] + self.n] == 0:
                self.assign(c[0], None)
        else:
            self.watches[c[0] + self.n].append(c)
            self.watches[c[1] + self.n].append(c)

    def note(self, lits, delete=False):
        if self.proof is not None:
            self.proof.append(("d " if delete else "") + " ".join(map(str, list(lits) + [0])))

    def assign(self, lit, reason):
        v = abs(lit)
        self.val[lit + self.n], self.val[-lit + self.n] = 1, -1
        self.level[v], self.reason[v] = len(self.limits), reason
        self.trail.append(lit)

    def propagate(self):
        n, val, watches = self.n, self.val, self.watches
        while self.qhead < len(self.trail):
            false = -self.trail[self.qhead]
            self.qhead += 1
            ws, keep = watches[false + n], []
            for k, c in enumerate(ws):
                if c[0] == false:
                    c[0], c[1] = c[1], c[0]  # the false watch always sits at c[1]
                if val[c[0] + n] == 1:
                    keep.append(c)
                    continue
                for m in range(2, len(c)):
                    if val[c[m] + n] != -1:
                        c[1], c[m] = c[m], c[1]
                        watches[c[1] + n].append(c)
                        break
                else:
                    keep.append(c)
                    if val[c[0] + n] == -1:
                        watches[false + n] = keep + ws[k + 1:]
                        return c
                    self.assign(c[0], c)
                    self.stats["propagations"] += 1
            watches[false + n] = keep
        return None

    def analyze(self, conflict):
        # Walk the trail back from the conflict until one literal of this level is left: the first UIP.
        level, seen, here = self.level, self.seen, len(self.limits)
        learnt, count, p, i = [0], 0, None, len(self.trail) - 1
        reason = conflict
        while True:
            for q in reason:
                v = abs(q)
                if q != p and not seen[v] and level[v] > 0:
                    seen[v] = True
                    self.bump(v)
                    if level[v] == here:
                        count += 1
                    else:
                        learnt.append(q)
            while not seen[abs(self.trail[i])]:
                i -= 1
            p = self.trail[i]
            i -= 1
            seen[abs(p)] = False
            count -= 1
            if count == 0:
                break
            reason = self.reason[abs(p)]
        learnt[0] = -p
        full = learnt
        if self.minimizing:
            learnt = self.minimize(learnt)  # while seen still marks the whole clause
            self.stats["minimized"] += len(full) - len(learnt)
        for q in full[1:]:
            seen[abs(q)] = False
        if len(learnt) == 1:
            return learnt, 0
        top = max(range(1, len(learnt)), key=lambda k: level[abs(learnt[k])])
        learnt[1], learnt[top] = learnt[top], learnt[1]
        return learnt, level[abs(learnt[1])]

    def minimize(self, learnt):
        # Drop each literal whose falsity follows from the rest of the clause. DOCS.md has the why.
        levels = 0  # one bit per decision level in the clause, folded into 64
        for q in learnt[1:]:
            levels |= 1 << (self.level[abs(q)] & 63)
        memo = {}
        return [learnt[0]] + [q for q in learnt[1:] if not self.redundant(q, levels, memo)]

    def redundant(self, q, levels, memo):
        # Depth first through reasons with an explicit stack; memo[v] says whether v follows from the clause.
        level, reason, seen = self.level, self.reason, self.seen
        if reason[abs(q)] is None:
            return False  # a decision follows from nothing
        stack = [(abs(q), 0)]
        while stack:
            v, k = stack.pop()
            r = reason[v]
            while k < len(r):
                u = abs(r[k])
                k += 1
                if u == v or seen[u] or level[u] == 0 or memo.get(u):
                    continue
                if memo.get(u) is False or reason[u] is None or not levels >> (level[u] & 63) & 1:
                    for w, _ in stack:
                        memo[w] = False  # everything on the path needed u
                    memo[v] = False
                    return False
                stack.append((v, k))
                stack.append((u, 0))
                break
            else:
                memo[v] = True
        return True

    def bump(self, v):
        self.activity[v] += self.bump_by
        if self.activity[v] > 1e100:
            self.activity = [a * 1e-100 for a in self.activity]
            self.bump_by *= 1e-100
            self.rebuild_heap()

    def decay_activity(self):
        # Growing the bump is the same as shrinking every older bump, without touching them.
        self.bump_by /= self.decay

    def rebuild_heap(self):
        self.heap = [(-self.activity[v], v) for v in range(1, self.n + 1) if self.val[v + self.n] == 0]
        heapq.heapify(self.heap)

    def backtrack(self, level):
        if len(self.limits) <= level:
            return
        start, n = self.limits[level], self.n
        for lit in self.trail[start:]:
            v = abs(lit)
            self.val[lit + n] = self.val[-lit + n] = 0
            self.reason[v] = None
            self.phase[v] = lit > 0
            heapq.heappush(self.heap, (-self.activity[v], v))
        del self.trail[start:]
        del self.limits[level:]
        self.qhead = start

    def pick(self):
        # The heap holds stale entries too; an entry counts only if its activity is current.
        if len(self.heap) > 10 * self.n + 1000:
            self.rebuild_heap()
        while self.heap:
            act, v = heapq.heappop(self.heap)
            if self.val[v + self.n] == 0 and -act == self.activity[v]:
                return v if self.phase[v] else -v
        return None

    def reduce(self):
        # Of the worse half by LBD, delete all but glue clauses (LBD 2 or less) and reasons. DOCS.md says why.
        self.reduce_every += self.reduce_inc
        self.next_reduce = self.stats["conflicts"] + self.reduce_every
        locked = {id(self.reason[abs(l)]) for l in self.trail}
        order = sorted(range(len(self.learnts)), key=lambda i: -self.lbds[i])
        gone = {i for i in order[:len(order) // 2] if self.lbds[i] > 2 and id(self.learnts[i]) not in locked}
        dead = [self.learnts[i] for i in gone]
        for c in dead:
            self.note(c, delete=True)
        ids, n = {id(c) for c in dead}, self.n
        for w in {c[0] + n for c in dead} | {c[1] + n for c in dead}:
            self.watches[w] = [c for c in self.watches[w] if id(c) not in ids]
        self.learnts = [c for i, c in enumerate(self.learnts) if i not in gone]
        self.lbds = [b for i, b in enumerate(self.lbds) if i not in gone]
        self.stats["reductions"] += 1
        self.stats["deleted"] += len(gone)

    def solve(self, max_conflicts=None):
        if not self.ok:
            self.note([])
            return None
        stats, restarts, since = self.stats, 1, 0
        while True:
            conflict = self.propagate()
            if conflict:
                stats["conflicts"] += 1
                since += 1
                if not self.limits:
                    self.note([])  # the empty clause: a conflict with nothing decided
                    return None
                if max_conflicts and stats["conflicts"] > max_conflicts:
                    raise RuntimeError(f"gave up after {max_conflicts} conflicts")
                learnt, back = self.analyze(conflict)
                self.lbds.append(len({self.level[abs(l)] for l in learnt}))  # before the jump changes levels
                self.backtrack(back)
                self.learnts.append(learnt)
                self.note(learnt)
                stats["learned"] += 1
                if len(learnt) > 1:
                    self.watches[learnt[0] + self.n].append(learnt)
                    self.watches[learnt[1] + self.n].append(learnt)
                self.assign(learnt[0], learnt if len(learnt) > 1 else None)
                self.decay_activity()
            elif since >= self.restart_unit * luby(restarts):
                stats["restarts"] += 1
                restarts += 1
                since = 0
                self.backtrack(0)
            elif self.next_reduce and stats["conflicts"] >= self.next_reduce:
                self.reduce()
            else:
                lit = self.pick()
                if lit is None:
                    return [v if self.val[v + self.n] == 1 else -v for v in range(1, self.n + 1)]
                stats["decisions"] += 1
                self.limits.append(len(self.trail))
                self.assign(lit, None)


def dpll(nvars, clauses, stats=None):
    # Plain DPLL for comparison: rescan every clause to propagate, branch on the lowest free variable.
    stats = stats if stats is not None else {}
    for key in ("decisions", "conflicts", "propagations"):
        stats.setdefault(key, 0)
    assign, trail = {}, []

    def propagate():
        changed = True
        while changed:
            changed = False
            for c in clauses:
                free, nfree = None, 0
                for l in c:
                    value = assign.get(abs(l))
                    if value is None:
                        free, nfree = l, nfree + 1
                    elif value == (l > 0):
                        break
                else:
                    if nfree == 0:
                        return False
                    if nfree == 1:
                        assign[abs(free)] = free > 0
                        trail.append(abs(free))
                        stats["propagations"] += 1
                        changed = True
        return True

    def undo(mark):
        while len(trail) > mark:
            del assign[trail.pop()]

    def search():
        mark = len(trail)
        if not propagate():
            stats["conflicts"] += 1
            undo(mark)
            return False
        v = next((v for v in range(1, nvars + 1) if v not in assign), None)
        if v is None:
            return True
        for value in (True, False):
            stats["decisions"] += 1
            assign[v] = value
            trail.append(v)
            if search():
                return True
            undo(mark)
        return False

    if not search():
        return None
    return [v if assign.get(v, False) else -v for v in range(1, nvars + 1)]


def count_models(nvars, clauses, limit=None):
    # Every solution, found by solving again with each one forbidden by a clause it alone breaks.
    clauses, found = list(clauses), 0
    while limit is None or found < limit:
        model, _ = solve(nvars, clauses)
        if model is None:
            break
        found += 1
        clauses.append([-lit for lit in model])
    return found


def verify(clauses, text):
    # Checks a solver's v lines against the formula: the first clause no literal satisfies, or None.
    # A variable the answer leaves out counts as unset, so it satisfies nothing.
    true = {int(t) for line in text.splitlines() if line.startswith("v ") for t in line.split()[1:]} - {0}
    if any(-lit in true for lit in true):
        raise ValueError("the answer sets a variable both ways")
    return next((c for c in clauses if not true.intersection(c)), None)


def check_proof(clauses, text):
    # Forward DRUP checking: each added clause, once all its literals are made false, must let unit
    # propagation reach a conflict, and the proof must end in the empty clause. Returns None or why not.
    if any(not c for c in clauses):
        return None  # the formula is false as it stands
    db, occurs, ids, units, fresh = {}, {}, {}, set(), itertools.count()

    def conflicts(assumed):
        true, queue = set(), []
        for lit in assumed + [db[i][0] for i in units]:
            if -lit in true:
                return True
            if lit not in true:
                true.add(lit)
                queue.append(lit)
        while queue:
            lit = queue.pop()
            for i in occurs.get(-lit, ()):
                c = db.get(i)
                if c is None or any(x in true for x in c):
                    continue
                free = [x for x in c if -x not in true]
                if not free:
                    return True
                if len(free) == 1:
                    true.add(free[0])
                    queue.append(free[0])
        return False

    def insert(c):
        i = next(fresh)
        db[i] = c
        ids.setdefault(frozenset(c), []).append(i)
        for lit in c:
            occurs.setdefault(lit, []).append(i)
        if len(c) == 1:
            units.add(i)

    for c in clauses:
        insert(list(dict.fromkeys(c)))
    for number, line in enumerate(text.splitlines(), 1):
        words = line.split()
        if not words or words[0] == "c":
            continue
        delete = words[0] == "d"
        try:
            lits = [int(w) for w in words[delete:]]
        except ValueError:
            raise ValueError(f"line {number}: not a clause: {line.strip()}")
        if not lits or lits[-1] != 0 or 0 in lits[:-1]:
            raise ValueError(f"line {number}: a clause must end in 0, and only there")
        lits = list(dict.fromkeys(lits[:-1]))
        if delete:
            same = ids.get(frozenset(lits))
            if not same:
                return f"line {number} deletes a clause that is not there: {line.strip()}"
            i = same.pop()
            del db[i]
            units.discard(i)
            continue
        if not conflicts([-l for l in lits]):
            return f"line {number} does not follow by unit propagation: {line.strip()}"
        if not lits:
            return None
        insert(lits)
    return "the proof never reaches the empty clause"


def resolvents(pos, neg, v):
    # Every clause that follows from one with v and one with -v, leaving out the always-true ones.
    for p in pos:
        for q in neg:
            r = dict.fromkeys([l for l in p if l != v] + [l for l in q if l != -v])
            if not any(-l in r for l in r):
                yield list(r)


def eliminate(nvars, clauses, proof=None, longest=20):
    # Bounded variable elimination, as in SatELite: a variable goes when the resolvents on it are no more
    # clauses than the ones holding it. Returns the clauses left and what extend() needs. DOCS.md has why.
    db, occurs, fresh, removed = {}, {l: set() for l in range(-nvars, nvars + 1)}, itertools.count(), []
    for c in clauses:
        c = list(dict.fromkeys(c))
        if not any(-l in c for l in c):
            i = next(fresh)
            db[i] = c
            for l in c:
                occurs[l].add(i)
    # Cheapest first; after a pass, only variables whose clauses changed are worth trying again.
    touched = set(range(1, nvars + 1))
    while touched:
        queue, touched = sorted(touched, key=lambda v: len(occurs[v]) * len(occurs[-v])), set()
        for v in queue:
            here = occurs[v] | occurs[-v]
            if not here:
                continue
            pos, neg = [db[i] for i in occurs[v]], [db[i] for i in occurs[-v]]
            added = list(itertools.islice(resolvents(pos, neg, v), len(here) + 1))
            if len(added) > len(here) or any(len(r) > longest for r in added):
                continue
            for r in added:  # each follows by unit propagation from its two parents, so add before deleting them
                if proof is not None:
                    proof.append(" ".join(map(str, r + [0])))
                i = next(fresh)
                db[i] = r
                for l in r:
                    occurs[l].add(i)
            for i in here:
                if proof is not None:
                    proof.append("d " + " ".join(map(str, db[i] + [0])))
                for l in db.pop(i):
                    occurs[l].discard(i)
                    touched.add(abs(l))
            touched.discard(v)
            removed.append((v, pos))
            if [] in added:
                return list(db.values()), removed
    return list(db.values()), removed


def extend(model, removed):
    # Values for the eliminated variables, newest first: false, unless a clause it was taken from needs it
    # true. Never both ways, since then the resolvent of the two clauses would be false too.
    true = set(model)
    for v, pos in reversed(removed):
        need = any(not any(l in true for l in c if l != v) for c in pos)
        true.discard(-v if need else v)
        true.add(v if need else -v)
    return [v if v in true else -v for v in range(1, len(model) + 1)]


def solve(nvars, clauses, method="cdcl", max_conflicts=None, keep=False, minimize=True, proof=None, elim=False):
    start = time.perf_counter()
    removed = []
    if elim:
        clauses, removed = eliminate(nvars, clauses, proof)
    if method == "dpll":
        stats = {}
        model = dpll(nvars, clauses, stats)
    else:
        solver = Solver(nvars, clauses, reduce_first=None if keep else 2000, minimize=minimize, proof=proof)
        model = solver.solve(max_conflicts)
        stats = solver.stats
    if model is not None and removed:
        model = extend(model, removed)
    stats["eliminated"] = len(removed)
    stats["time"] = time.perf_counter() - start
    return model, stats


def queens(n):
    square = lambda r, c: r * n + c + 1
    clauses = [[square(r, c) for c in range(n)] for r in range(n)]  # a queen in every row
    cells = [(r, c) for r in range(n) for c in range(n)]
    for i, (r1, c1) in enumerate(cells):
        for r2, c2 in cells[i + 1:]:
            if r1 == r2 or c1 == c2 or abs(r1 - r2) == abs(c1 - c2):
                clauses.append([-square(r1, c1), -square(r2, c2)])
    return n * n, clauses


def sudoku(grid):
    # Variable 81r + 9c + d says "cell (r, c) holds digit d"; grid is 81 chars, "." or "0" for blanks.
    cell = lambda r, c, d: 81 * r + 9 * c + d
    groups = [[(r, c) for c in range(9)] for r in range(9)]
    groups += [[(r, c) for r in range(9)] for c in range(9)]
    groups += [[(br + r, bc + c) for r in range(3) for c in range(3)] for br in (0, 3, 6) for bc in (0, 3, 6)]
    clauses = []
    for r in range(9):
        for c in range(9):
            clauses.append([cell(r, c, d) for d in range(1, 10)])
            clauses += [[-cell(r, c, d), -cell(r, c, e)] for d in range(1, 10) for e in range(d + 1, 10)]
            if grid[9 * r + c] not in ".0":
                clauses.append([cell(r, c, int(grid[9 * r + c]))])
    for group in groups:
        for d in range(1, 10):
            clauses.append([cell(r, c, d) for r, c in group])
            clauses += [[-cell(*a, d), -cell(*b, d)] for i, a in enumerate(group) for b in group[i + 1:]]
    return 729, clauses


def sudoku_grid(model):
    digits = {(v - 1) // 9: (v - 1) % 9 + 1 for v in model if v > 0}
    return "".join(str(digits[i]) for i in range(81))


def main():
    ap = argparse.ArgumentParser(description="A CDCL SAT solver.")
    ap.add_argument("file", nargs="?", help="a DIMACS CNF file")
    ap.add_argument("--stats", action="store_true", help="print decisions, conflicts and time as c lines")
    ap.add_argument("--dpll", action="store_true", help="use the plain DPLL solver instead")
    ap.add_argument("--keep", action="store_true", help="never delete learned clauses")
    ap.add_argument("--elim", action="store_true", help="remove variables by resolution before searching")
    ap.add_argument("--no-minimize", action="store_true", help="keep learned clauses as first-UIP analysis left them")
    ap.add_argument("--queens", type=int, metavar="N", help="place N queens on an N by N board")
    ap.add_argument("--sudoku", metavar="GRID", help="81 characters, '.' or '0' for blanks")
    ap.add_argument("--cnf", action="store_true", help="print the formula as DIMACS CNF instead of solving it")
    ap.add_argument("--count", action="store_true", help="count every solution instead of finding one")
    ap.add_argument("--verify", metavar="ANSWER", help="check a solver's s and v output against the formula")
    ap.add_argument("--proof", metavar="FILE", help="write a DRAT proof of an UNSAT answer to FILE")
    ap.add_argument("--check-proof", metavar="FILE", help="check a DRUP proof that the formula has no solution")
    args = ap.parse_args()
    if args.queens:
        nvars, clauses = queens(args.queens)
    elif args.sudoku:
        if len(args.sudoku) != 81:
            sys.exit("sat: a sudoku is 81 characters")
        nvars, clauses = sudoku(args.sudoku)
    elif args.file:
        try:
            with open(args.file) as f:
                nvars, clauses = parse_dimacs(f.read())
        except (OSError, ValueError) as e:
            sys.exit(f"sat: {args.file}: {e}")
    else:
        ap.error("give a CNF file, --queens or --sudoku")
    if args.cnf:
        # The formula itself in DIMACS, so another solver can be given exactly the same problem.
        print(f"p cnf {nvars} {len(clauses)}")
        print("\n".join(" ".join(map(str, c)) + " 0" for c in clauses))
        return
    if args.verify:
        try:
            with open(args.verify) as f:
                broken = verify(clauses, f.read())
        except (OSError, ValueError) as e:
            sys.exit(f"sat: {args.verify}: {e}")
        if broken is not None:
            sys.exit("sat: unsatisfied clause: " + " ".join(map(str, broken)) + " 0")
        print("c verified")
        return
    if args.check_proof:
        try:
            with open(args.check_proof) as f:
                why = check_proof(clauses, f.read())
        except (OSError, ValueError) as e:
            sys.exit(f"sat: {args.check_proof}: {e}")
        if why:
            sys.exit(f"sat: {args.check_proof}: {why}")
        print("c proof verified")
        return
    if args.count:
        print(f"c solutions {count_models(nvars, clauses)}")
        return
    if args.proof and args.dpll:
        ap.error("--proof needs the CDCL solver, not --dpll")
    proof = [] if args.proof else None
    model, stats = solve(nvars, clauses, "dpll" if args.dpll else "cdcl", keep=args.keep, minimize=not args.no_minimize,
                         proof=proof, elim=args.elim)
    if args.proof:
        with open(args.proof, "w") as f:
            f.write("".join(line + "\n" for line in proof))
    if args.stats:
        for key in ("eliminated", "decisions", "conflicts", "propagations", "learned", "minimized", "restarts", "deleted"):
            if key in stats:
                print(f"c {key} {stats[key]}")
        print(f"c time {stats['time']:.3f}s")
    if model is None:
        print("s UNSATISFIABLE")
        sys.exit(20)
    print("s SATISFIABLE")
    if args.queens:
        on = set(model)
        for r in range(args.queens):
            print("c " + " ".join("Q" if r * args.queens + c + 1 in on else "." for c in range(args.queens)))
    elif args.sudoku:
        grid = sudoku_grid(model)
        for r in range(9):
            print("c " + " ".join(grid[9 * r:9 * r + 9]))
    lits = model + [0]
    for i in range(0, len(lits), 20):
        print("v " + " ".join(map(str, lits[i:i + 20])))
    sys.exit(10)


if __name__ == "__main__":
    main()
