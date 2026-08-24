cmake_minimum_required(VERSION 3.16)
project(cmake_proj C)

# 静态库：目标函数所在。控制器在 goaloop-build 子目录注入
# -fsanitize=address,undefined -fprofile-instr-generate -fcoverage-mapping 构建。
add_library(cmake_target STATIC src/target.c)
target_include_directories(cmake_target PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
