// SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// A downstream of nlohmann/json: it compiles against the library's usage
// requirements and prints what they gave it. JSON_DIAGNOSTICS changes the
// exception text (the JSON pointer of the failing value) and the layout of
// basic_json (a parent pointer), so it is an ABI- and behaviour-affecting
// define that must reach every consumer the same way (upstream #3106).
#include <iostream>
#include <nlohmann/json.hpp>

int main() {
    nlohmann::json j = {{"a", {{"b", 1}}}};
    std::cout << "version " << NLOHMANN_JSON_VERSION_MAJOR << "." << NLOHMANN_JSON_VERSION_MINOR
              << "." << NLOHMANN_JSON_VERSION_PATCH << "\n";
    std::cout << "JSON_DIAGNOSTICS=" << JSON_DIAGNOSTICS << "\n";
    try {
        (void)j["a"]["b"].get<std::string>();
    } catch (const nlohmann::json::type_error& e) {
        std::cout << e.what() << "\n";
    }
    return 0;
}
