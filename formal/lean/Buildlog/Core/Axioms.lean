-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Core.Tools
import Buildlog.Core.Export
import Buildlog.Core.FunctionalTools

#print axioms Buildlog.Core.ConfigInput.minimum_unique
#print axioms Buildlog.Core.ConfigInput.exec_settings_functional
#print axioms Buildlog.Core.ConfigInput.exec_settings_preserves_keys
#print axioms Buildlog.Core.ConfigInput.exec_targets_host
#print axioms Buildlog.Core.ConfigInput.exec_targets_the_host
#print axioms Buildlog.Core.ConfigInput.exec_config_functional
#print axioms Buildlog.Core.ConfigInput.exec_config_idempotent
#print axioms Buildlog.Core.ToolchainInput.selection_exact
#print axioms Buildlog.Core.ToolchainInput.selection_has_capabilities
#print axioms Buildlog.Core.ToolchainInput.multiple_unpinned_candidates_error
#print axioms Buildlog.Core.ToolchainInput.conflicting_pins_error
#print axioms Buildlog.Core.ToolchainInput.noncandidate_pin_error
#print axioms Buildlog.Core.ToolchainInput.toolchain_uniqueness
#print axioms Buildlog.Core.ToolchainInput.no_silent_choice
#print axioms Buildlog.Core.ToolchainInput.missing_selection_errors_every_action
#print axioms Buildlog.Core.ToolchainInput.no_selection_errors_every_action
#print axioms Buildlog.Core.ToolchainInput.required_selection_outcome
#print axioms Buildlog.Core.ToolInput.direct_dependencies_exact
#print axioms Buildlog.Core.ToolInput.direct_tool_need_exact
#print axioms Buildlog.Core.ToolInput.needed_minimal
#print axioms Buildlog.Core.ToolInput.dependency_minimal
#print axioms Buildlog.Core.ToolInput.no_roots_no_needed
#print axioms Buildlog.Core.ToolInput.tool_configuration_invariant
#print axioms Buildlog.Core.ToolInput.typed_reference_unique_host_instance
#print axioms Buildlog.Core.ToolInput.built_toolchain_tools_needed
#print axioms Buildlog.Core.ToolInput.scalar_path_needs_host
#print axioms Buildlog.Core.ToolInput.list_path_needs_host
#print axioms Buildlog.Core.ToolInput.built_path_uses_host
#print axioms Buildlog.Core.ToolInput.missing_tool_artifact_error
#print axioms Buildlog.Core.ExportInput.export_property_coverage
#print axioms Buildlog.Core.ExportInput.error_free_lossless_properties_spelled
#print axioms Buildlog.Core.ExportInput.overlapping_tables_error
#print axioms Buildlog.Core.ExportInput.foreign_rules_lost_with_warning
#print axioms Buildlog.Core.export_usage_roundtrip
#print axioms Buildlog.Core.ConfigInput.minimum_exists
#print axioms Buildlog.Core.ConfigInput.minimum_idempotent
#print axioms Buildlog.Core.ConfigInput.inherit_override_preserves_value
#print axioms Buildlog.Core.ConfigInput.exec_config_functional_of_host
#print axioms Buildlog.Core.DeclarationInput.bad_reports_error
#print axioms Buildlog.Core.DeclarationInput.exec_value_exists
#print axioms Buildlog.Core.DeclarationInput.error_free_reserved_absent
#print axioms Buildlog.Core.DeclarationInput.key_priority_unique
#print axioms Buildlog.Core.DeclarationInput.rank_declared
#print axioms Buildlog.Core.DeclarationInput.base_settings_declared
#print axioms Buildlog.Core.DeclarationInput.base_entries
#print axioms Buildlog.Core.DeclarationInput.exec_settings_total
#print axioms Buildlog.Core.DeclarationInput.base_exec_total
#print axioms Buildlog.Core.DeclarationInput.base_exec_exists_unique
#print axioms Buildlog.Core.ExecutionContext.known_exec_total
#print axioms Buildlog.Core.ExecutionContext.tool_spec
#print axioms Buildlog.Core.ExecutionContext.tool_unique
#print axioms Buildlog.Core.ExecutionContext.tool_host
#print axioms Buildlog.Core.ExecutionContext.tool_idempotent
#print axioms Buildlog.Core.ToolInput.tool_configuration_functional
#print axioms Buildlog.Core.ToolInput.built_tools_functional
#print axioms Buildlog.Core.ExportInput.row_spelled_projects
#print axioms Buildlog.Core.ExportInput.rejected_row_lost_with_warning
#print axioms Buildlog.Core.ExportInput.error_free_row_exclusive
#print axioms Buildlog.Core.ExportInput.error_free_E1_row_spelled
#print axioms Buildlog.Core.ExportInput.no_loss_alone_insufficient
