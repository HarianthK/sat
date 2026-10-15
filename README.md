# sat

A SAT solver written from scratch, in one Python file with no dependencies. Give
it a formula in conjunctive normal form and it either finds values for the
variables that make every clause true, or says that none exist.

    $ python sat.py --queens 8 --stats
    c decisions 35
    c conflicts 14
    c propagations 285
    c learned 14
    c restarts 0
    c time 0.002s
    s SATISFIABLE
    c . . . . . . . Q
    c . Q . . . . . .
    c . . . . Q . . .
    c . . Q . . . . .
    c Q . . . . . . .
    c . . . . . . Q .
    c . . . Q . . . .
    c . . . . . Q . .
    v -1 -2 -3 -4 -5 -6 -7 8 -9 10 -11 -12 -13 -14 -15 -16 -17 -18 -19 -20
    ...

Sixty-four variables, one per square, and 736 clauses: a queen in every row,
and no two on the same row, column or diagonal. The solver knows nothing about
chess. Fourteen times it walked into a dead end, and each time it wrote down
why as a new clause, so it never walked into that one again.

## What it does

- Reads DIMACS CNF, the format every SAT solver reads: comments, the `p cnf`
  line, clauses ending in 0 that may span lines, and SATLIB's `%` ending.
  Broken files are refused with the line number and a reason.
- CDCL, the algorithm behind every modern solver: unit propagation with two
  watched literals, conflict analysis to the first unique implication point,
  learned clauses, and backjumping past the levels that did not matter.
- VSIDS-style branching: variables in recent conflicts are tried first, with
  older bumps decaying by 5% per conflict. The last value of each variable is
  remembered and reused (phase saving).
- Restarts on the Luby sequence, 100 conflicts times 1, 1, 2, 1, 1, 2, 4, ...
- A plain DPLL solver too, `--dpll`, kept for comparison.
- Answers in SAT competition form: `s` and `v` lines, exit status 10 or 20.
- Two encoders for fun: N-queens and Sudoku.

## How it compares

Conflicts and time against the plain DPLL in the same file, on the first
unsatisfiable random formula of each size and on pigeonhole formulas. One
Python process, median of five runs:

| instance | DPLL conflicts | DPLL time | CDCL conflicts | CDCL time |
| --- | --- | --- | --- | --- |
| random 3-SAT, 40 variables | 293 | 0.070s | 49 | 0.004s |
| random 3-SAT, 60 variables | 2,711 | 1.30s | 138 | 0.012s |
| random 3-SAT, 100 variables | 20,659 | 16.6s | 434 | 0.053s |
| 7 pigeons, 6 holes | 3,246 | 0.45s | 805 | 0.12s |
| 8 pigeons, 7 holes | 32,781 | 6.35s | 3,654 | 1.69s |

On random formulas CDCL needs 6 to 48 times fewer conflicts here, and the gap
grows with size. Pigeonhole stays hard for both, which is the point of it;
DOCS.md says why.

## Running it

    python sat.py FILE.cnf [--stats] [--dpll]
    python sat.py --queens N
    python sat.py --sudoku 8..........36......7..9.2...5...7.......457.....1...3...1....68..85...1..9....4..
    python test_sat.py

## Notes

[DOCS.md](DOCS.md) is what writing it taught me, including the 25 ways I broke
it on purpose and which check caught each one.
