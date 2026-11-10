# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# A plain CMake downstream of an installed nlohmann/json: find_package in
# CONFIG mode with a version request, then the namespaced target.
cmake_minimum_required(VERSION 3.16)
project(nlohmann_downstream LANGUAGES CXX)
find_package(nlohmann_json 3.2 CONFIG REQUIRED)
add_executable(app ../main.cpp)
target_link_libraries(app PRIVATE nlohmann_json::nlohmann_json)
