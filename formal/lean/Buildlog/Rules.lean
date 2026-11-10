-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Semantics

namespace Buildlog
open Facts

/-- Finite ground rule instances. `test` contains constraints and positive,
negative or aggregate reads of answers and complete lower strata. Only
positive recursive premises read `j`, the current-stratum interpretation.
`reads` conservatively records all answer identifiers inspected by `test`. -/
structure GroundStratum (n m : Nat) where
  count : Nat
  head : Fin count → Fin m
  positive : Fin count → Facts m
  test : Fin count → Facts n → Facts m → Prop
  reads : Fin count → Facts n
  lowerReads : Fin count → Facts m
  test_ext : ∀ r e e' lower lower',
    (∀ i, reads r i → (e i ↔ e' i)) →
    (∀ i, lowerReads r i → (lower i ↔ lower' i)) →
    (test r e lower ↔ test r e' lower')

namespace GroundStratum

theorem test_local (p : GroundStratum n m) (r : Fin p.count) (e x : Facts n)
    (lower : Facts m) (absent : ∀ i, x i → ¬ p.reads r i) :
    p.test r e lower ↔ p.test r (union e x) lower := by
  apply p.test_ext
  · intro i hi
    simp only [union]
    have hn : ¬ x i := fun hx => absent i hx hi
    simp only [hn, or_false]
  · intro _ _; rfl

def fire (p : GroundStratum n m) (r : Fin p.count) (e : Facts n)
    (lower j : Facts m) : Prop :=
  p.test r e lower ∧ Sub (p.positive r) j

def operator (p : GroundStratum n m) (e : Facts n) (lower j : Facts m) : Facts m :=
  union j (fun h => ∃ r, p.head r = h ∧ p.fire r e lower j)

def positiveSystem (p : GroundStratum n m) (e : Facts n) (lower : Facts m) : Positive m :=
  { step := p.operator e lower
    grows := fun _ _ hi => Or.inl hi
    mono := by
      intro s t h i hi
      rcases hi with old | ⟨r, head, test, premises⟩
      · exact Or.inl (h i old)
      · exact Or.inr ⟨r, head, test, sub_trans premises h⟩ }

/-- A direct request guard is a sufficient syntactic certificate. Auxiliary
guard certification is left to the loader interface, not presumed here. -/
theorem guarded_operator (p : GroundStratum n m)
    (requestFact : Fin n → Fin m) (guarded : Facts n)
    (guard : ∀ r i, p.reads r i → guarded i → p.positive r (requestFact i))
    (e x : Facts n) (lower j : Facts m)
    (gx : Sub x guarded) (absent : ∀ i, x i → ¬ j (requestFact i)) :
    p.operator e lower j = p.operator (union e x) lower j := by
  have tests : ∀ r, Sub (p.positive r) j →
      (p.test r e lower ↔ p.test r (union e x) lower) := by
    intro r hp
    apply p.test_local
    intro i hxi hread
    exact absent i hxi (hp _ (guard r i hread (gx i hxi)))
  apply Facts.ext
  intro h
  constructor
  · rintro (old | ⟨r, hh, ht, hp⟩)
    · exact Or.inl old
    · exact Or.inr ⟨r, hh, (tests r hp).mp ht, hp⟩
  · rintro (old | ⟨r, hh, ht, hp⟩)
    · exact Or.inl old
    · exact Or.inr ⟨r, hh, (tests r hp).mpr ht, hp⟩

end GroundStratum

/-- A finite grounded, stratified program with direct guards. The rank fields
record the syntactic well-formedness of head and positive premise strata.
`test` can inspect only the complete lower interpretation and answer input. -/
structure GroundProgram (n m : Nat) where
  height : Nat
  rank : Fin m → Nat
  rules : Fin height → GroundStratum n m
  head_rank : ∀ s r, rank ((rules s).head r) = s.val
  positive_rank : ∀ s r i, (rules s).positive r i → rank i ≤ s.val
  lower_rank : ∀ s r i, (rules s).lowerReads r i → rank i < s.val
  requestFact : Fin n → Fin m
  guarded : Facts n
  guards : ∀ s r i, (rules s).reads r i → guarded i →
    (rules s).positive r (requestFact i)

def GroundProgram.semantics (p : GroundProgram n m) : Stratified n m :=
  { height := p.height
    stratum := fun s e lower => (p.rules s).positiveSystem e lower
    requests := fun j i => j (p.requestFact i)
    requests_mono := fun h _ hi => h _ hi
    guarded := p.guarded
    guarded_local := fun s e x lower j gx absent =>
      (p.rules s).guarded_operator p.requestFact p.guarded (p.guards s) e x lower j gx absent }

/-- Rule-level instantiation, not a postulated final-model inertness axiom. -/
theorem ground_inert_answers (p : GroundProgram n m) (e x : Facts n)
    (gx : Sub x p.guarded)
    (absent : (∀ i, x i → ¬ p.semantics.requests (p.semantics.model e) i) ∨
      (∀ i, x i → ¬ p.semantics.requests (p.semantics.model (union e x)) i)) :
    p.semantics.model e = p.semantics.model (union e x) :=
  p.semantics.inert_answers e x gx absent

end Buildlog
