-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Restarts

namespace Buildlog
open Facts

/-- The actual driver tracks (directory, reason), not just directories.
The finite offer index may be the union of finite pass results over all root
sets; deterministic passes and finitely many root sets make that union finite. -/
structure OfferState (n r : Nat) where
  roots : Facts n
  seen : Facts r

theorem OfferState.ext {s t : OfferState n r}
    (roots : s.roots = t.roots) (seen : s.seen = t.seen) : s = t := by
  cases s
  cases t
  simp_all

def offerStep (offers : Facts n → Facts r) (owner : Fin r → Fin n)
    (s : OfferState n r) : OfferState n r :=
  { roots := union s.roots (fun i => ∃ j, offers s.roots j ∧ ¬ s.seen j ∧ owner j = i)
    seen := union s.seen (offers s.roots) }

theorem offer_roots_grow (offers : Facts n → Facts r) (owner : Fin r → Fin n)
    (s : OfferState n r) : Sub s.roots (offerStep offers owner s).roots :=
  fun _ hi => Or.inl hi

/-- A new reason for a waiting directory may stutter once, never twice. -/
theorem stutter_cannot_repeat (offers : Facts n → Facts r) (owner : Fin r → Fin n)
    (s : OfferState n r) (same : (offerStep offers owner s).roots = s.roots) :
    offerStep offers owner (offerStep offers owner s) = offerStep offers owner s := by
  apply OfferState.ext
  · change union (offerStep offers owner s).roots
      (fun i => ∃ j, offers (offerStep offers owner s).roots j ∧
        ¬ (offerStep offers owner s).seen j ∧ owner j = i) =
        (offerStep offers owner s).roots
    rw [same]
    apply Facts.ext
    intro i
    simp only [offerStep, union]
    grind
  · change union (union s.seen (offers s.roots))
      (offers (offerStep offers owner s).roots) = union s.seen (offers s.roots)
    rw [same]
    apply Facts.ext
    intro j
    simp only [union]
    grind

theorem offer_loop_terminates (offers : Facts n → Facts r) (owner : Fin r → Fin n)
    (s : OfferState n r) :
    ∃ k, k ≤ n + r ∧
      iterate (offerStep offers owner) (k + 1) s = iterate (offerStep offers owner) k s := by
  have progress : ∀ s : OfferState n r, offerStep offers owner s ≠ s →
      card s.roots + card s.seen <
        card (offerStep offers owner s).roots + card (offerStep offers owner s).seen := by
    intro s neq
    have roots_grow := offer_roots_grow offers owner s
    have seen_grow : Sub s.seen (offerStep offers owner s).seen := fun _ hi => Or.inl hi
    have cr := card_le roots_grow
    have cs := card_le seen_grow
    have witness : (∃ i, (offerStep offers owner s).roots i ∧ ¬ s.roots i) ∨
        (∃ j, (offerStep offers owner s).seen j ∧ ¬ s.seen j) := by
      apply Classical.byContradiction
      intro hn
      apply neq
      apply OfferState.ext
      · apply sub_antisymm
        · intro i hi
          apply Classical.byContradiction
          intro old
          exact hn (Or.inl ⟨i, hi, old⟩)
        · exact roots_grow
      · apply sub_antisymm
        · intro j hj
          apply Classical.byContradiction
          intro old
          exact hn (Or.inr ⟨j, hj, old⟩)
        · exact seen_grow
    rcases witness with roots | seen
    · have := card_strict roots_grow roots; omega
    · have := card_strict seen_grow seen; omega
  obtain ⟨k, hk, heq⟩ := bounded_progress (n := n + r) (offerStep offers owner)
    (fun s => card s.roots + card s.seen)
    (fun s => by have := card_bound s.roots; have := card_bound s.seen; omega)
    progress s
  exact ⟨k, by omega, heq⟩

end Buildlog
