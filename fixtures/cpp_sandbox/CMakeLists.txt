cmake_minimum_required(VERSION 3.20)
project(cpp_sandbox CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(MSVC)
  add_compile_options(/W4)
else()
  add_compile_options(-Wall -Wextra)
endif()

add_library(sandbox
  src/ring_buffer.cpp
  src/text_util.cpp
)
target_include_directories(sandbox PUBLIC include)

enable_testing()

foreach(name ring_buffer text_util fd_owner slow)
  add_executable(test_${name} tests/test_${name}.cpp)
  target_link_libraries(test_${name} PRIVATE sandbox)
  add_test(NAME ${name} COMMAND test_${name})
endforeach()

set_tests_properties(slow PROPERTIES TIMEOUT 5)
