cmake_minimum_required(VERSION 3.20)

project(lca_native_endpoint VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(LCA_ENDPOINT_TESTS "Build the endpoint test suite" ON)
option(LCA_WARNINGS_AS_ERRORS "Treat compiler warnings as errors" OFF)
option(LCA_HARDENING "Compile and link with the hardening flags in engineering-standards.md" ON)
set(LCA_OPENVINO_GENAI "AUTO" CACHE STRING "Build the OpenVINO GenAI backend: AUTO, ON or OFF")
set_property(CACHE LCA_OPENVINO_GENAI PROPERTY STRINGS AUTO ON OFF)
set(LCA_SANITIZE "" CACHE STRING "Sanitizers for GCC/Clang builds, for example address,undefined")

include(cmake/third_party_headers.cmake)
include(CheckPIESupported)
check_pie_supported()

# ---------------------------------------------------------------- warnings

# internal/docs/engineering-standards.md owns these rules; this is where C++ meets them.
function(lca_warnings target)
  if(MSVC)
    target_compile_options(${target} PRIVATE /W4 /permissive- /utf-8)
    if(LCA_WARNINGS_AS_ERRORS)
      target_compile_options(${target} PRIVATE /WX)
    endif()
  else()
    target_compile_options(${target} PRIVATE
      -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion
      -Wnull-dereference -Wdouble-promotion -Wformat=2 -Wimplicit-fallthrough)
    target_compile_options(${target} PRIVATE
      $<$<COMPILE_LANGUAGE:CXX>:-Wold-style-cast -Wnon-virtual-dtor -Woverloaded-virtual>)
    if(LCA_WARNINGS_AS_ERRORS)
      target_compile_options(${target} PRIVATE -Werror)
    endif()
  endif()
  lca_hardening(${target})
  if(LCA_SANITIZE AND NOT MSVC)
    target_compile_options(${target} PRIVATE -fsanitize=${LCA_SANITIZE} -fno-omit-frame-pointer)
    target_link_options(${target} PRIVATE -fsanitize=${LCA_SANITIZE})
  endif()
endfunction()

function(lca_hardening target)
  if(NOT LCA_HARDENING)
    return()
  endif()
  if(MSVC)
    target_compile_options(${target} PRIVATE /sdl /guard:cf)
    target_link_options(${target} PRIVATE /guard:cf /DYNAMICBASE /HIGHENTROPYVA /NXCOMPAT)
    return()
  endif()
  target_compile_options(${target} PRIVATE
    -fstack-protector-strong -fstack-clash-protection
    $<$<COMPILE_LANGUAGE:CXX>:-D_GLIBCXX_ASSERTIONS>)
  # FORTIFY needs optimisation and conflicts with ASan's interceptors, so it is
  # applied to optimised, unsanitised builds only. Undefine first: some toolchains
  # predefine a weaker level.
  if(NOT LCA_SANITIZE)
    target_compile_options(${target} PRIVATE
      $<$<NOT:$<CONFIG:Debug>>:-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=3>)
  endif()
  set_target_properties(${target} PROPERTIES POSITION_INDEPENDENT_CODE ON)
  if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_options(${target} PRIVATE -Wl,-z,relro,-z,now -Wl,-z,noexecstack)
  endif()
endfunction()

# ---------------------------------------------------------------- OpenVINO GenAI

set(LCA_HAVE_OPENVINO_GENAI OFF)
if(NOT LCA_OPENVINO_GENAI STREQUAL "OFF")
  if(LCA_OPENVINO_GENAI STREQUAL "ON")
    find_package(OpenVINOGenAI REQUIRED)
  else()
    find_package(OpenVINOGenAI QUIET)
  endif()
  if(OpenVINOGenAI_FOUND)
    # The backend also calls the runtime API directly (device name, version).
    find_package(OpenVINO REQUIRED COMPONENTS Runtime)
    set(LCA_HAVE_OPENVINO_GENAI ON)
  endif()
endif()
message(STATUS "lca-endpoint: OpenVINO GenAI backend ${LCA_HAVE_OPENVINO_GENAI}")

# ---------------------------------------------------------------- core library

find_package(Threads REQUIRED)

add_library(lca_endpoint_core STATIC
  src/chat_format.cpp
  src/endpoint_server.cpp
  src/fixture_backend.cpp
  src/inference_scheduler.cpp
  src/openai_protocol.cpp
  src/output_parser.cpp
  src/plugin_backend.cpp
  src/process_memory.cpp
)
target_include_directories(lca_endpoint_core
  PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)
# SYSTEM must directly follow the target name. Anywhere else CMake reads it as a
# directory called "SYSTEM", and the third-party headers are warned on like our own.
target_include_directories(lca_endpoint_core SYSTEM PUBLIC ${LCA_THIRD_PARTY_INCLUDE_DIR})
target_compile_definitions(lca_endpoint_core PUBLIC
  LCA_ENDPOINT_VERSION="${PROJECT_VERSION}"
  "LCA_BUILD_TYPE=\"$<CONFIG>\""
)
target_link_libraries(lca_endpoint_core PUBLIC Threads::Threads ${CMAKE_DL_LIBS})
if(WIN32)
  # _CRT_SECURE_NO_WARNINGS: std::getenv is used read-only at startup.
  target_compile_definitions(lca_endpoint_core PUBLIC _WIN32_WINNT=0x0A00 NOMINMAX WIN32_LEAN_AND_MEAN
                                                      _CRT_SECURE_NO_WARNINGS)
  target_link_libraries(lca_endpoint_core PUBLIC ws2_32 psapi)
endif()
if(LCA_HAVE_OPENVINO_GENAI)
  target_sources(lca_endpoint_core PRIVATE src/openvino_genai_backend.cpp)
  target_compile_definitions(lca_endpoint_core PUBLIC LCA_WITH_OPENVINO_GENAI)
  target_link_libraries(lca_endpoint_core PUBLIC openvino::genai openvino::runtime)
endif()
lca_warnings(lca_endpoint_core)

add_executable(lca-endpoint src/main.cpp)
target_link_libraries(lca-endpoint PRIVATE lca_endpoint_core)
lca_warnings(lca-endpoint)

# ---------------------------------------------------------------- reference plugin

add_library(lca_reference_echo_backend SHARED plugins/reference_echo_backend.c)
target_include_directories(lca_reference_echo_backend PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
set_target_properties(lca_reference_echo_backend PROPERTIES C_VISIBILITY_PRESET hidden)
lca_warnings(lca_reference_echo_backend)

install(TARGETS lca-endpoint lca_reference_echo_backend
  RUNTIME DESTINATION bin
  LIBRARY DESTINATION lib
  ARCHIVE DESTINATION lib)
install(FILES include/lca/backend_plugin.h DESTINATION include/lca)

# ---------------------------------------------------------------- tests

if(LCA_ENDPOINT_TESTS)
  enable_testing()
  add_executable(lca_endpoint_tests
    tests/test_main.cpp
    tests/test_chat_format.cpp
    tests/test_endpoint_server.cpp
    tests/test_inference_scheduler.cpp
    tests/test_openai_protocol.cpp
    tests/test_output_parser.cpp
    tests/test_plugin_backend.cpp
  )
  target_link_libraries(lca_endpoint_tests PRIVATE lca_endpoint_core)
  target_compile_definitions(lca_endpoint_tests PRIVATE
    "LCA_REFERENCE_PLUGIN_PATH=\"$<TARGET_FILE:lca_reference_echo_backend>\"")
  add_dependencies(lca_endpoint_tests lca_reference_echo_backend)
  lca_warnings(lca_endpoint_tests)
  add_test(NAME lca_endpoint_tests COMMAND lca_endpoint_tests)
endif()
