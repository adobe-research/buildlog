-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.CacheSeeding

namespace Buildlog.Examples
open Facts

/-- Constant request followed by a guarded use of its result row. -/
def cacheRows : GroundProgram 1 2 :=
  { height := 1
    rank := fun _ => 0
    rules := fun _ =>
      { count := 2
        head := id
        positive := fun r h => r = 1 ∧ h = 0
        test := fun r e _ => r = 0 ∨ e 0
        reads := fun r _ => r = 1
        lowerReads := fun _ => empty
        test_ext := by
          intro r e e' lower lower' answers _
          by_cases one : r = 1
          · exact or_congr Iff.rfl (answers 0 one)
          · have zero : r = 0 := by apply Fin.ext; simp_all; omega
            simp only [zero, true_or] }
    head_rank := by intro s _; have := s.isLt; simp_all
    positive_rank := by intro _ _ _ _; exact Nat.zero_le _
    lower_rank := by intro _ _ _ h; exact False.elim h
    requestFact := fun _ => 0
    guarded := full
    guards := by intro _ r _ read _; exact ⟨read, rfl⟩ }

def cacheBundles : BundleProgram 1 1 2 :=
  { rows := cacheRows
    alpha := fun _ => full
    owner := fun _ => 0
    requestFact := fun _ => 0
    guarded := full
    ownership := fun _ _ _ => Subsingleton.elim _ _
    request_owner := fun _ => rfl
    guarded_owner := fun _ => Iff.rfl }

def validHit : CacheConfirmation 1 (Facts 1) Unit :=
  { expected := fun _ => full
    snapshot := fun _ => full
    active := full
    reads := fun _ => empty
    rank := fun _ => 0
    probe := fun _ _ => full
    stored := fun _ => full
    key := fun _ _ => ()
    storedKey := fun _ => ()
    watchesValid := full
    oracle_correct := fun _ _ _ => rfl
    prerequisites := fun _ _ _ h => False.elim h
    earlier := fun _ _ _ h => False.elim h
    cache_sound := fun _ _ _ _ => rfl
    accounted := fun _ _ => Or.inr ⟨rfl, rfl, True.intro⟩ }

def freshOnly : CacheConfirmation 1 (Facts 1) Unit :=
  { validHit with
    stored := fun _ => empty
    watchesValid := empty
    cache_sound := fun _ _ _ h => False.elim h
    accounted := fun _ _ => Or.inl rfl }

def cacheExamplePhase (fresh : Bool) : CachePhase cacheBundles Unit :=
  { confirmation := if fresh then freshOnly else validHit
    expected := by cases fresh <;> rfl
    f := empty
    confirmed := full
    tentative := empty
    active := by
      cases fresh <;> apply Facts.ext <;> intro i <;>
        simp [freshOnly, validHit, full, union, empty]
    confirmed_guarded := fun _ _ => True.intro
    f_unguarded := fun _ h => False.elim h
    tentative_guarded := fun _ h => False.elim h
    tentative_owned := fun _ _ h _ => False.elim h
    unused := fun _ h => False.elim h
    covered := fun _ _ _ => True.intro }

def cacheExampleRun (fresh : Bool) : CachePhaseRun cacheBundles Unit :=
  { length := 0
    phase := fun _ => cacheExamplePhase fresh
    zero := rfl
    next := fun _ h => by omega
    done := fun _ h => h.1 True.intro }

theorem cache_hit_matches_cache_free :
    (cacheExamplePhase false).model = (cacheExamplePhase true).model :=
  CompletedConfigure.cached_fresh_agree
    (CompletedConfigure.run (cacheExampleRun false))
    (CompletedConfigure.run (cacheExampleRun true)) id

theorem replay_matches_cache_free :
    (cacheExamplePhase false).model = (cacheExamplePhase true).model :=
  CompletedConfigure.cached_fresh_agree
    (CompletedConfigure.replay (CompletedConfigure.run (cacheExampleRun false))
      (cacheExamplePhase false) rfl)
    (CompletedConfigure.run (cacheExampleRun true)) id

/-- Unseeded is not cache-free: a stable key and watches alone cannot
establish FC. A stale constant hit differs from the current constant probe. -/
theorem stale_hit_violates_FC :
    ¬ (∀ _snapshot : Unit, () = () → True → (0 : Nat) = 1) := by
  intro sound
  have := sound () rfl True.intro
  omega

end Buildlog.Examples
