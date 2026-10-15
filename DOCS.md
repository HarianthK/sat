# What I learned writing a SAT solver

## The problem is small and the search is not

A formula in CNF is a list of clauses, each clause a list of literals: `3` means
variable 3 is true, `-3` that it is false. A clause is satisfied if any one of its
literals is. With n variables there are 2^n assignments, and the whole field is
about not trying them. Sudoku becomes 729 variables and 11,988 clauses; 2^729 is
not an option, and the solver here finishes the hard one in 55 conflicts.

## DPLL first, because CDCL is DPLL with a memory

The plain version is short. Pick a variable, try true, and if that fails try
false. Before each choice, unit propagation: any clause with every literal false
but one forces that last one. That is all `dpll` does. It rescans every clause
each time it propagates, which is honest and slow, and when it fails it backs up
one choice and forgets why it failed. CDCL changes both.

## Why two watched literals

Propagation is where nearly all the time goes, and the question it asks is "is
this clause now unit, or false?" A clause cannot become unit while two of its
literals are not false. So each clause watches two such literals and is only
looked at when one of those becomes false. Then it either finds another literal
that is not false to watch instead, or it is unit (or a conflict) on the other
watch.

The pleasant surprise is backtracking: it costs nothing. Undoing assignments can
only make literals un-false, which can never break the "two not false" promise,
so no watch moves when the solver backs up. Clauses whose watches never become
false are never touched at all. On the Inkala sudoku DPLL spends 0.45 s for 18
conflicts, rescanning about 12,000 clauses at every step; CDCL spends 0.036 s for
55.

The one detail that matters: in `propagate` the false watch is always swapped
into position 1, so position 0 is the other watch and, when the clause forces
something, the forced literal. A forced literal's reason clause therefore has it
first, which conflict analysis relies on.

## What a learned clause says

When a clause comes out all false, the solver walks back through the reasons. Each
forced literal was forced by a clause, so each can be replaced by the literals
that forced it: "x is true because a and b were false". Keep replacing and you
get a set of earlier assignments that together lead to the conflict. The learned
clause is the negation of that set: "not all of these at once". It is implied by
the original formula, never a new fact, just one that took work to see, and the
test checks exactly that on 330 small formulas: every learned clause must hold in
every model brute force finds.

## Where to stop: the first UIP

Replace too little and the clause is long and specific; replace everything and
you are back to the decisions, which is what DPLL learns implicitly. The standard
stop is the first unique implication point: walk the current level backwards
until exactly one literal of that level is left in the clause. That literal is
a single point every path from the decision to the conflict passes through.

It makes the clause asserting. After jumping back to the second highest level in
it, every literal but that one is false, so the clause immediately forces it. The
solver does not decide its way back: it learns and lands on a forced
assignment.

`analyze` is twenty lines and they are the solver. `count` is how many literals
of the current level are still waiting to be resolved away; walking the trail
backwards and skipping anything not `seen` visits them in reverse order of
assignment, and the walk stops when `count` hits zero.

## Backjumping is why levels exist

The jump goes to the second highest level in the learned clause, not one level
back. Decisions in between had nothing to do with the conflict, and they are
dropped with everything they forced. Over six random formulas of 40 and 60
variables the test sees 39 jumps of more than one level.

Jumping back only one level, chronologically, turns out to be correct. The
learned clause is still unit there. It is just slower: on the ten formulas
below it took 20,348 conflicts against 16,106. Jumping too far is wrong, and
fast to show: the clause is not unit yet, so the "forced" literal is a guess
with a reason attached, and four-variable formulas came back UNSAT.

## The heuristic, measured, not assumed

Each conflict bumps every variable that took part in it, and the bump grows by
1/0.95 each conflict, which is the same as decaying every older bump by 5%
without touching them. The next decision takes the free variable with the
highest score from a heap.

I expected decay to be the important part. Measured over ten instances (pigeonhole
8 into 7, and three random formulas each of 100, 125 and 150 variables), total
conflicts:

| variant | pigeonhole 8 into 7 | nine random formulas |
| --- | --- | --- |
| everything on | 3,654 | 12,452 |
| no bumping at all | 322 | 250,158 |
| bumping, no decay | 2,986 | (15,264 on twelve; 14,883 with decay) |
| chronological backjumping | 4,589 | 15,759 |
| no restarts | 2,943 | 14,522 |

Bumping is what matters: without it the random formulas take 20 times as many
conflicts, 116,983 against 2,826 for one of them. Decay made no real difference
on these. Pigeonhole goes the other way: with no bumping at all it took 322
conflicts. My guess is that its symmetry makes a fixed order as good as any and
the bumping only stirs it, but I have not checked that. These are small Python
instances; real solvers report decay mattering on large industrial ones, which I
have none of.

## Why restarts help, and here mostly do not

A restart throws away every decision and keeps every learned clause and score.
The idea is that an early bad choice can trap the search in a hard corner, and
starting over with what has been learned lets the heuristic pick a better
first move. Phase saving makes it cheap: each variable gets back its last value,
so the solver rebuilds most of where it was in a few propagations.

Restarts here follow the Luby sequence times 100 conflicts: 1, 1, 2, 1, 1, 2, 4,
1, 1, ... It is the schedule that is provably within a log factor of the best
fixed one when you do not know which that is. Measured, restarts saved about 14%
of conflicts on the random formulas and cost 24% on pigeonhole. The formulas that
restarts are meant for, with structure and heavy-tailed run times, are not ones I
had here, so the honest finding is "no harm on average, not shown to help".

## Pigeonhole is hard by mathematics

n+1 pigeons in n holes cannot fit, and a person sees why at once: count them. A
solver cannot count. Clauses say "each pigeon is in some hole" and "no hole holds
two"; every proof by resolution, and a CDCL run is one, needs a number of steps
exponential in n (Haken proved this in 1985). Learning cannot shortcut it:

| pigeons into holes | 5 into 4 | 6 into 5 | 7 into 6 | 8 into 7 |
| --- | --- | --- | --- | --- |
| CDCL conflicts | 28 | 145 | 805 | 3,654 |
| DPLL conflicts | 52 | 375 | 3,246 | 32,781 |

Each extra hole is four to six times the conflicts for CDCL, seven to ten for
DPLL. That makes it a good test, too: the answer is UNSAT by counting, not because
my solver said so, and a broken solver that learns one clause too many finds that
out quickly.

## CDCL against DPLL

The first unsatisfiable random formula at 4.26 clauses per variable for each size,
in one process, median of five runs:

| instance | DPLL conflicts | DPLL time | CDCL conflicts | CDCL time |
| --- | --- | --- | --- | --- |
| random 3-SAT, 20 variables | 28 | 0.003s | 15 | 0.001s |
| random 3-SAT, 40 variables | 293 | 0.070s | 49 | 0.004s |
| random 3-SAT, 60 variables | 2,711 | 1.30s | 138 | 0.012s |
| random 3-SAT, 80 variables | 2,411 | 1.54s | 192 | 0.021s |
| random 3-SAT, 100 variables | 20,659 | 16.6s | 434 | 0.053s |
| pigeonhole 8 into 7 | 32,781 | 6.35s | 3,654 | 1.69s |
| 8 queens | 24 | 0.011s | 14 | 0.002s |
| Inkala sudoku | 18 | 0.454s | 55 | 0.036s |

Two things are mixed in the time column. Conflicts measure the search: on the
random and pigeonhole formulas learning and backjumping cut them by 2 to 48 times.
On the two puzzles, which are easy for both, it is about even, and the sudoku
took CDCL 55 conflicts against DPLL's 18. Time per conflict measures propagation:
DPLL rescans every clause, so on that sudoku it found fewer dead ends and still
took 12 times as long. A random formula at a given size varies a lot, which is
why 80 variables came out easier than 60 for DPLL. At 125 variables DPLL was still
running after several minutes, and I stopped it.

## The checks, and how each was shown to work

No check trusts the solver's own structures. A SATISFIABLE answer is checked
against every clause with a plain set of true literals. UNSAT is checked three
ways: brute force on 330 random formulas of 4 to 14 variables, pigeonhole
formulas that are unsatisfiable by counting, and N-queens for 2 and 3. Counting
solutions is the best check of all: forbidding each answer and solving again must
find exactly 10, 4, 40 and 92 boards for 5 to 8 queens, which are known, and
exactly one solution to each sudoku, the published one. A solver that wrongly
says UNSAT halfway loses solutions and the count comes out short.

At these small sizes most random formulas at 4.26 are satisfiable (269 of 330):
the 50% point sits higher for small n and moves to 4.26 as n grows.

Then each check was shown to fail. A script copied the solver, broke one thing,
ran the tests, and put it back. 25 breakages, each one alone:

| breakage | what caught it |
| --- | --- |
| a watch not moved to the new literal | a model that breaks a clause, 4 variables |
| conflict analysis stopping one literal early | learned `[-3]`, not implied by the formula |
| learned clause missing a literal | learned `[2, 3]`, not implied |
| backjumping one level too far | UNSAT where brute force found a model |
| backjumping one level only (chronological) | the first-UIP check: jumped to 10, should be 7 |
| decay never called | activity never decayed after thousands of conflicts |
| decay doing nothing | the same |
| restarts never happening | pigeonhole 8 into 7 never restarted |
| Luby sequence off by one | the sequence check |
| `seen` flags not cleared after analysis | UNSAT where brute force found a model |
| propagation queue not reset on backtrack | a model that breaks a clause |
| a unit clause in the input ignored | pigeonhole 2 into 1 "satisfied" |
| a false clause not reported as a conflict | hangs in `propagate`; the watchdog kills it |
| forced literal stored without its reason | crash in analysis |
| conflict analysis bumping nothing | 8,116 conflicts on three formulas, budget 4,000 |
| a clause cannot span lines | the parser check |
| SATLIB's `%` ending not handled | the parser check |
| a literal past the declared variables accepted | the parser check |
| DPLL never trying false | DPLL disagrees with brute force |
| DPLL missing a false clause | DPLL's model breaks a clause |
| N-queens without the diagonal rule | 2 queens "satisfiable" |
| sudoku without the box rule | 101 solutions where there is one |
| `v` lines dropping the last variable | the command-line model check |
| UNSAT exiting 0 instead of 20 | the command-line check |
| propagations not counted | the stats check |

Five of them passed or hung the first time, which is the useful part:

- Chronological backjumping passed everything, because it is correct. Only a
  check of the jump itself catches it, so the test now watches every conflict:
  the learned clause has exactly one literal from the conflict level, and the
  jump goes to the highest level among the rest.
- Bumping nothing passed too, also correct, just slow. The test now runs three
  125-variable formulas measured at 1,532 conflicts with bumping and 8,116
  without, and allows 4,000.
- Sudoku without boxes hung: the solution counter had no limit and the puzzle
  had thousands of answers. It stops at 100 now.
- A conflict not reported hung inside `propagate`, which a conflict budget
  cannot stop because no conflict is ever counted. `faulthandler` now kills the
  test after 120 seconds with a traceback of where it was.
- The propagation count was only checked to be printed, not to be counted.

One I caught before running it. The first decay check bumped two variables and
did the decay itself, in the test, so removing decay from `solve` would have
passed. Decay is now a method that `solve` calls, and the pigeonhole check asks
that thousands of conflicts left the bump larger than it started.

The solver itself passed every test on its first run, so there is no bug story to
tell from writing it. The breakages are the evidence that the tests would have
told me if there had been one.

## What is not here

Learned clauses are never deleted, so memory grows with conflicts; at Python's
speed that has not mattered yet, and it is the next thing a larger instance would
need. There is no clause minimization (removing literals implied by others in the
learned clause) and no preprocessing.
