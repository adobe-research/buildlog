-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Rules
import Buildlog.Closure

namespace Buildlog
open Facts

/-- RL and I at the finite row/bundle boundary. A key owns all its result
rows, and each row's request guard is its owner's request. -/
structure BundleProgram (n a m : Nat) where
  rows : GroundProgram a m
  alpha : Fin n → Facts a
  owner : Fin a → Fin n
  requestFact : Fin n → Fin m
  guarded : Facts n
  ownership : ∀ i r, alpha i r → owner r = i
  request_owner : ∀ r, rows.requestFact r = requestFact (owner r)
  guarded_owner : ∀ r, rows.guarded r ↔ guarded (owner r)

namespace BundleProgram

def flatten (p : BundleProgram n a m) (e : Facts n) : Facts a :=
  fun r => ∃ i, e i ∧ p.alpha i r

theorem flatten_union (p : BundleProgram n a m) (e f : Facts n) :
    p.flatten (union e f) = union (p.flatten e) (p.flatten f) := by
  apply Facts.ext
  intro r
  simp only [flatten, union]
  grind

def requests (p : BundleProgram n a m) (j : Facts m) : Facts n :=
  fun i => j (p.requestFact i)

/-- Fixed correct bundles induce a stratified semantics from the row rules.
Guardedness is proved from those rules, not assumed as final-model inertness. -/
def semantics (p : BundleProgram n a m) : Stratified n m :=
  { height := p.rows.height
    stratum := fun s e lower => (p.rows.rules s).positiveSystem (p.flatten e) lower
    requests := p.requests
    requests_mono := fun h _ hi => h _ hi
    guarded := p.guarded
    guarded_local := by
      intro s e x lower j gx absent
      change (p.rows.rules s).operator (p.flatten e) lower j =
        (p.rows.rules s).operator (p.flatten (union e x)) lower j
      rw [p.flatten_union]
      apply (p.rows.rules s).guarded_operator p.rows.requestFact p.rows.guarded
        (p.rows.guards s)
      · rintro r ⟨i, hx, ha⟩
        apply (p.guarded_owner r).mpr
        rw [p.ownership i r ha]
        exact gx i hx
      · rintro r ⟨i, hx, ha⟩ present
        apply absent i hx
        change j (p.requestFact i)
        simpa only [p.request_owner r, p.ownership i r ha] using present }

theorem model_flatten (p : BundleProgram n a m) (e : Facts n) :
    p.semantics.model e = p.rows.semantics.model (p.flatten e) := by
  have stages : ∀ k (h : k ≤ p.rows.height),
      p.semantics.stages e k h =
        p.rows.semantics.stages (p.flatten e) k h := by
    intro k
    induction k with
    | zero => intro h; rfl
    | succ k ih =>
      intro h
      change ((p.rows.rules ⟨k, by omega⟩).positiveSystem (p.flatten e)
        (p.semantics.stages e k (by change k ≤ p.rows.height; omega))).model
          (p.semantics.stages e k (by change k ≤ p.rows.height; omega)) =
        ((p.rows.rules ⟨k, by omega⟩).positiveSystem (p.flatten e)
          (p.rows.semantics.stages (p.flatten e) k
            (by change k ≤ p.rows.height; omega))).model
            (p.rows.semantics.stages (p.flatten e) k
              (by change k ≤ p.rows.height; omega))
      rw [ih (by omega)]
  exact stages p.rows.height (Nat.le_refl _)

end BundleProgram
end Buildlog
