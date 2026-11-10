-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Core.Tools
import Buildlog.Core.Declarations

namespace Buildlog.Core.ToolInput

/-- Functional prop:exec for the checked declaration model. Membership in the
configuration domain replaces the old exec-destination existence premise. -/
theorem tool_configuration_functional (i : ToolInput) (ctx : ExecutionContext)
    (compatible : i.tc.config = ctx.input) (c : ctx.Configuration)
    {t a r use g : Symbol}
    (reference : Ref i t c.val a r use) (resolved : Resolves i t r g) :
    Needed i g (ctx.tool c).val ∧ (ctx.tool c).val.platform = ctx.host := by
  have exec : ConfigInput.Exec i.tc.config c.val (ctx.tool c).val := by
    rw [compatible]
    exact ctx.tool_spec c
  exact ⟨Needed.tool (Dependency.reference reference resolved) exec, ctx.tool_host c⟩

theorem built_tools_functional (i : ToolInput) (ctx : ExecutionContext)
    (compatible : i.tc.config = ctx.input) (c : ctx.Configuration)
    {t tc role g : Symbol} (needed : Needed i t c.val)
    (chosen : i.targetToolchain t tc) (program : i.tc.toolTarget tc role g) :
    Needed i g (ctx.tool c).val := by
  apply Needed.tool (Dependency.builtToolchain needed chosen program)
  rw [compatible]
  exact ctx.tool_spec c

end Buildlog.Core.ToolInput
