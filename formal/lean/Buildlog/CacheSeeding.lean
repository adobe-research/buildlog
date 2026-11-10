-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Bundles
import Buildlog.CacheConfirmation

namespace Buildlog
open Facts

def snapshotRows (snapshot : Fin n → Facts a) (keys : Facts n) : Facts a :=
  fun r => ∃ i, keys i ∧ snapshot i r

theorem snapshotRows_union (snapshot : Fin n → Facts a) (keys other : Facts n) :
    snapshotRows snapshot (union keys other) =
      union (snapshotRows snapshot keys) (snapshotRows snapshot other) := by
  apply Facts.ext
  intro r
  simp only [snapshotRows, union]
  grind

/-- A completed guarded phase. Arbitrary tentative seed rows remain in the
model, but have absent requests after confirmation and stale-seed removal.
Checks on correct subsets (CK) and the Python protocol's extraction of this
certificate are outside the finite ground-rule representation. -/
structure CachePhase (p : BundleProgram n a m) (Key : Type) where
  confirmation : CacheConfirmation n (Facts a) Key
  expected : confirmation.expected = p.alpha
  f : Facts n
  confirmed : Facts n
  tentative : Facts n
  active : confirmation.active = union confirmed f
  confirmed_guarded : Sub confirmed p.guarded
  f_unguarded : ∀ i, f i → ¬ p.guarded i
  tentative_guarded : Sub tentative p.guarded
  tentative_owned : ∀ i r, tentative i → confirmation.snapshot i r → p.owner r = i
  unused : ∀ i, tentative i →
    ¬ p.requests (p.rows.semantics.model
      (snapshotRows confirmation.snapshot (union confirmation.active tentative))) i
  covered : ∀ i, p.guarded i →
    p.requests (p.rows.semantics.model
      (snapshotRows confirmation.snapshot (union confirmation.active tentative))) i →
    confirmed i

namespace CachePhase
variable {n a m : Nat} {p : BundleProgram n a m} {Key : Type}

def input (c : CachePhase p Key) : Facts a :=
  snapshotRows c.confirmation.snapshot (union c.confirmation.active c.tentative)

noncomputable def model (c : CachePhase p Key) : Facts m :=
  p.rows.semantics.model c.input

theorem active_rows_correct (c : CachePhase p Key) :
    snapshotRows c.confirmation.snapshot c.confirmation.active =
      p.flatten (union c.confirmed c.f) := by
  have correct : ∀ i, c.confirmation.active i →
      c.confirmation.snapshot i = p.alpha i := by
    intro i hi
    exact (c.confirmation.active_answers_correct i hi).trans (congrFun c.expected i)
  apply Facts.ext
  intro r
  constructor
  · rintro ⟨i, hi, row⟩
    exact ⟨i, by simpa only [c.active] using hi, by simpa only [correct i hi] using row⟩
  · rintro ⟨i, hi, row⟩
    have active : c.confirmation.active i := by simpa only [c.active] using hi
    exact ⟨i, active, by simpa only [correct i active] using row⟩

/-- The phase lemma with FC/FL confirmation discharged, including arbitrary
wrong inactive seeds. RL turns their absent owner requests into row guards. -/
theorem reduced (c : CachePhase p Key) :
    p.semantics.Closed c.f c.confirmed ∧
      c.model = p.semantics.model (union (p.semantics.leastAnswers c.f) c.f) := by
  have input_eq : c.input =
      union (p.flatten (union c.confirmed c.f))
        (snapshotRows c.confirmation.snapshot c.tentative) := by
    unfold input
    rw [snapshotRows_union, c.active_rows_correct]
  have inert := p.rows.semantics.inert_answers
    (p.flatten (union c.confirmed c.f))
    (snapshotRows c.confirmation.snapshot c.tentative)
    (by
      rintro r ⟨i, hi, row⟩
      apply (p.guarded_owner r).mpr
      rw [c.tentative_owned i r hi row]
      exact c.tentative_guarded i hi)
    (Or.inr (by
      rintro r ⟨i, hi, row⟩ present
      apply c.unused i hi
      change p.rows.semantics.model c.input (p.requestFact i)
      change p.rows.semantics.model
        (union (p.flatten (union c.confirmed c.f))
          (snapshotRows c.confirmation.snapshot c.tentative)) (p.rows.requestFact r)
        at present
      simpa only [← input_eq, p.request_owner r, c.tentative_owned i r hi row]
        using present))
  have clean_model : c.model = p.semantics.model (union c.confirmed c.f) := by
    rw [p.model_flatten]
    exact (congrArg p.rows.semantics.model input_eq).trans inert.symm
  have closed : p.semantics.Closed c.f c.confirmed := by
    refine ⟨c.confirmed_guarded, ?_⟩
    intro i hi
    apply c.covered i hi.1
    change p.requests c.model i
    rw [clean_model]
    exact hi.2
  exact ⟨closed, clean_model.trans
    ((p.semantics.least_closed_guarded_answers c.f).2.2.2 _ closed)⟩

end CachePhase

namespace BundleProgram

noncomputable def phaseModel (p : BundleProgram n a m) (f : Facts n) : Facts m :=
  p.semantics.model (union (p.semantics.leastAnswers f) f)

noncomputable def packagePending (p : BundleProgram n a m) (f : Facts n) : Facts n :=
  fun i => ¬ p.guarded i ∧ p.requests (p.phaseModel f) i ∧ ¬ f i

noncomputable def packageStep (p : BundleProgram n a m) (f : Facts n) : Facts n :=
  union f (p.packagePending f)

noncomputable def packageRounds (p : BundleProgram n a m) (k : Nat) : Facts n :=
  iterate p.packageStep k empty

theorem package_halt_iff (p : BundleProgram n a m) (f : Facts n) :
    (∀ i, ¬ p.packagePending f i) ↔ p.packageStep f = f := by
  constructor
  · intro halt
    apply Facts.ext
    intro i
    exact ⟨fun hi => hi.elim id (fun hp => False.elim (halt i hp)), Or.inl⟩
  · intro eq i pending
    have : p.packageStep f i := Or.inr pending
    rw [eq] at this
    exact pending.2.2 this

/-- Package demand is a deterministic increasing schedule, not a monotone
request operator or a least unguarded fixpoint. -/
theorem package_termination (p : BundleProgram n a m) :
    ∃ k, k ≤ n ∧ ∀ i, ¬ p.packagePending (p.packageRounds k) i := by
  have progress : ∀ f, p.packageStep f ≠ f →
      card f < card (p.packageStep f) := by
    intro f changed
    have pending : ∃ i, p.packagePending f i := by
      apply Classical.byContradiction
      intro none
      exact changed ((p.package_halt_iff f).mp (fun i hi => none ⟨i, hi⟩))
    obtain ⟨i, hi⟩ := pending
    exact card_strict (fun _ h => Or.inl h) ⟨i, Or.inr hi, hi.2.2⟩
  obtain ⟨k, hk, eq⟩ := bounded_progress p.packageStep card card_bound progress empty
  exact ⟨k, by omega, (p.package_halt_iff _).mpr
    ((iterate_step p.packageStep k empty).symm.trans eq)⟩

end BundleProgram

/-- Phase observations of a successful fresh or seeded run. Unguarded
answers start empty; only after guarded closure do all pending packages run.
Fresh phases use the probe alternative of `accounted`; an unseeded but
cache-using run still needs the FC alternative. -/
structure CachePhaseRun (p : BundleProgram n a m) (Key : Type) where
  length : Nat
  phase : Nat → CachePhase p Key
  zero : (phase 0).f = empty
  next : ∀ k, k < length → (phase (k + 1)).f =
    union (phase k).f
      (fun i => ¬ p.guarded i ∧ p.requests (phase k).model i ∧ ¬ (phase k).f i)
  done : ∀ i, ¬ (¬ p.guarded i ∧
    p.requests (phase length).model i ∧ ¬ (phase length).f i)

namespace CachePhaseRun

theorem follows_schedule (run : CachePhaseRun p Key) :
    ∀ k, k ≤ run.length → (run.phase k).f = p.packageRounds k := by
  intro k
  induction k with
  | zero => intro _; exact run.zero
  | succ k ih =>
    intro bound
    rw [run.next k (by omega), (run.phase k).reduced.2, ih (by omega)]
    exact (iterate_step p.packageStep k empty).symm

theorem terminal (run : CachePhaseRun p Key) :
    ∀ i, ¬ p.packagePending (p.packageRounds run.length) i := by
  have done := run.done
  rw [(run.phase run.length).reduced.2, run.follows_schedule _ (Nat.le_refl _)] at done
  exact done

end CachePhaseRun

theorem iterate_stable_from {State : Type} (step : State → State) (start : State)
    {k l : Nat} (fixed : step (iterate step k start) = iterate step k start)
    (later : k ≤ l) : iterate step l start = iterate step k start := by
  obtain ⟨d, rfl⟩ := Nat.exists_eq_add_of_le later
  induction d with
  | zero => simp
  | succ d ih =>
    rw [Nat.add_succ, iterate_step, ih (by omega), fixed]

namespace BundleProgram

theorem terminal_packages_unique (p : BundleProgram n a m) {k l : Nat}
    (hk : ∀ i, ¬ p.packagePending (p.packageRounds k) i)
    (hl : ∀ i, ¬ p.packagePending (p.packageRounds l) i) :
    p.packageRounds k = p.packageRounds l := by
  rcases Nat.le_total k l with le | le
  · exact (iterate_stable_from p.packageStep empty ((p.package_halt_iff _).mp hk) le).symm
  · exact iterate_stable_from p.packageStep empty ((p.package_halt_iff _).mp hl) le

noncomputable def finalPackages (p : BundleProgram n a m) : Facts n :=
  p.packageRounds (Classical.choose p.package_termination)

theorem completed_phase_run (p : BundleProgram n a m) (run : CachePhaseRun p Key) :
    (run.phase run.length).f = p.finalPackages ∧
      (run.phase run.length).model = p.phaseModel p.finalPackages := by
  have eq := p.terminal_packages_unique run.terminal
    (Classical.choose_spec p.package_termination).2
  have f := (run.follows_schedule _ (Nat.le_refl _)).trans eq
  exact ⟨f, by rw [(run.phase run.length).reduced.2, f]; rfl⟩

end BundleProgram

/-- Completed configure provenance. Replay retains the record's package
answers (not arbitrary unguarded seeds), validates current payloads by FC/FL,
and closes guarded demand. Neither `active` nor `confirmed` requires a
current request; withdrawn guarded and unguarded answers may be certified.
Deviation discards replay state; check failure
discards the speculative outcome and uses the retry only. -/
inductive CompletedConfigure (p : BundleProgram n a m) (Key : Type)
    : Facts n → Facts m → Prop
  | run (trace : CachePhaseRun p Key) :
      CompletedConfigure p Key (trace.phase trace.length).f (trace.phase trace.length).model
  | replay {f model} : CompletedConfigure p Key f model →
      (phase : CachePhase p Key) → phase.f = f →
      CompletedConfigure p Key phase.f phase.model
  | fallback {f model} : CompletedConfigure p Key f model →
      CompletedConfigure p Key f model

namespace CompletedConfigure
variable {n a m : Nat} {p : BundleProgram n a m} {Key Key' : Type}
  {f f' : Facts n} {model model' : Facts m}

theorem canonical (run : CompletedConfigure p Key f model) :
    f = p.finalPackages ∧ model = p.phaseModel p.finalPackages := by
  induction run with
  | run trace => exact p.completed_phase_run trace
  | replay _ phase packages ih =>
    have f := packages.trans ih.1
    exact ⟨f, by rw [phase.reduced.2, f]; rfl⟩
  | fallback _ ih => exact ih

/-- `cor:seeding` at the finite rule/bundle boundary: fresh, cached, seeded,
replayed and fallback outcomes agree after FC/FL confirmation. Rendering
reads non-result facts only, so withdrawn retained answers are immaterial. -/
theorem cached_fresh_agree (cached : CompletedConfigure p Key f model)
    (fresh : CompletedConfigure p Key' f' model') {Outcome : Type}
    (render : Facts m → Outcome) : render model = render model' := by
  rw [cached.canonical.2, fresh.canonical.2]

end CompletedConfigure
end Buildlog
