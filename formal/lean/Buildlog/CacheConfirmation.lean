-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Std

namespace Buildlog

/-- A completed confirmation/probing phase, not the Python protocol itself.
Payload may be a set-valued oracle bundle. The two provenance alternatives
mean a fresh call against this snapshot or a key/watch-validated cache hit. -/
structure CacheConfirmation (n : Nat) (Payload Key : Type) where
  expected : Fin n → Payload
  snapshot : Fin n → Payload
  active : Fin n → Prop
  reads : Fin n → Fin n → Prop
  rank : Fin n → Nat
  probe : Fin n → (Fin n → Payload) → Payload
  stored : Fin n → Payload
  key : Fin n → (Fin n → Payload) → Key
  storedKey : Fin n → Key
  watchesValid : Fin n → Prop
  /-- A/RL: correct prerequisite answers determine the fixed oracle bundle. -/
  oracle_correct : ∀ i s, (∀ j, reads i j → s j = expected j) →
    probe i s = expected i
  /-- FL's prerequisite closure and strict dependency ordering. Active means
  certified, not currently requested: replay can certify withdrawn answers. -/
  prerequisites : ∀ i j, active i → reads i j → active j
  earlier : ∀ i j, active i → reads i j → rank j < rank i
  /-- FC at a hit: equality of key and valid watches justify reuse against
  the current snapshot, not directly against already-correct α answers. -/
  cache_sound : ∀ i s, key i s = storedKey i → watchesValid i →
    stored i = probe i s
  accounted : ∀ i, active i →
    snapshot i = probe i snapshot ∨
      (snapshot i = stored i ∧ key i snapshot = storedKey i ∧ watchesValid i)

namespace CacheConfirmation

/-- FC/FL confirmation induction. Correctness of a dependency is derived
at its smaller rank, never assumed from incidental probe execution order.
Inactive tentative payloads may be arbitrary. -/
theorem active_answers_correct (c : CacheConfirmation n Payload Key) :
    ∀ i, c.active i → c.snapshot i = c.expected i := by
  have ranked : ∀ r i, c.rank i = r → c.active i → c.snapshot i = c.expected i := by
    intro r
    induction r using Nat.strongRecOn with
    | ind r ih =>
      intro i rank active
      have prerequisites : ∀ j, c.reads i j → c.snapshot j = c.expected j := by
        intro j read
        have less := c.earlier i j active read
        rw [rank] at less
        exact ih (c.rank j) less j rfl (c.prerequisites i j active read)
      have correct := c.oracle_correct i c.snapshot prerequisites
      rcases c.accounted i active with fresh | ⟨stored, key, watches⟩
      · exact fresh.trans correct
      · exact stored.trans ((c.cache_sound i c.snapshot key watches).trans correct)
  exact fun i active => ranked (c.rank i) i rfl active

end CacheConfirmation
end Buildlog
