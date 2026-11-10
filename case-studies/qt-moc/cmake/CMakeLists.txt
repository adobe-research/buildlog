# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The same consumer in CMake, for comparison (README.md, "Against CMake"):
#   cmake -G Ninja -S case-studies/qt-moc/cmake -B BUILD -DCMAKE_PREFIX_PATH=$BUILDLOG_QT_PREFIX
cmake_minimum_required(VERSION 3.21)
project(qt_moc_demo LANGUAGES CXX)

set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
find_package(Qt6 REQUIRED COMPONENTS Core Widgets)

set(SRC ${CMAKE_CURRENT_SOURCE_DIR}/../project)
add_library(counter STATIC ${SRC}/src/counter.cpp ${SRC}/src/counter.h ${SRC}/src/version.h)
target_include_directories(counter PUBLIC ${SRC}/src)
target_link_libraries(counter PUBLIC Qt6::Core)

add_executable(counter_test ${SRC}/tests/counter_test.cpp ${SRC}/tests/greeting.qrc)
target_link_libraries(counter_test PRIVATE counter Qt6::Widgets)
enable_testing()
add_test(NAME counter_test COMMAND counter_test)
