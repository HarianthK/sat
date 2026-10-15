# A CDCL SAT solver: two watched literals, first-UIP learning, backjumping, VSIDS and Luby restarts.
# Run: python sat.py FILE.cnf [--stats] [--dpll], or python sat.py --queens N, or --sudoku 81-CHARS
import argparse
import heapq
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
    def __init__(self, nvars, clauses, decay=0.95, restart_unit=100):
        n = self.n = nvars
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
        self.learnts = []
        self.stats = dict(decisions=0, conflicts=0, propagations=0, learned=0, restarts=0)
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
        for q in learnt[1:]:
            seen[abs(q)] = False
        if len(learnt) == 1:
            return learnt, 0
        top = max(range(1, len(learnt)), key=lambda k: level[abs(learnt[k])])
        learnt[1], learnt[top] = learnt[top], learnt[1]
        return learnt, level[abs(learnt[1])]

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

    def solve(self, max_conflicts=None):
        if not self.ok:
            return None
        stats, restarts, since = self.stats, 1, 0
        while True:
            conflict = self.propagate()
            if conflict:
                stats["conflicts"] += 1
                since += 1
                if not self.limits:
                    return None
                if max_conflicts and stats["conflicts"] > max_conflicts:
                    raise RuntimeError(f"gave up after {max_conflicts} conflicts")
                learnt, back = self.analyze(conflict)
                self.backtrack(back)
                self.learnts.append(learnt)
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


def solve(nvars, clauses, method="cdcl", max_conflicts=None):
    start = time.perf_counter()
    if method == "dpll":
        stats = {}
        model = dpll(nvars, clauses, stats)
    else:
        solver = Solver(nvars, clauses)
        model = solver.solve(max_conflicts)
        stats = solver.stats
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
    ap.add_argument("--queens", type=int, metavar="N", help="place N queens on an N by N board")
    ap.add_argument("--sudoku", metavar="GRID", help="81 characters, '.' or '0' for blanks")
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
    model, stats = solve(nvars, clauses, "dpll" if args.dpll else "cdcl")
    if args.stats:
        for key in ("decisions", "conflicts", "propagations", "learned", "restarts"):
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
