# A comment line (not counted).
cmake_minimum_required(VERSION 3.28)
project(Mini CXX)

#[[ A bracket comment
    spanning lines (not counted) ]]
add_library(Halide SHARED src/Func.cpp)  # trailing comments still count the line
#[==[ another ]==]

add_executable(binary2cpp tools/binary2cpp.cpp)
