# Third-party single-header dependencies, pinned by commit and SHA-256.
#
# Nothing third-party is committed to this repository. By default each header
# is downloaded once into the build tree from an immutable commit URL and its
# hash is checked. For offline builds, point LCA_THIRD_PARTY_DIR at a
# directory already holding the same files (same relative paths); their
# hashes are checked too, so an offline build uses exactly the pinned code.
#
#   cpp-httplib   v0.58.0   MIT   https://github.com/yhirose/cpp-httplib
#   nlohmann/json v3.12.0   MIT   https://github.com/nlohmann/json
#   doctest       v2.5.3    MIT   https://github.com/doctest/doctest   (tests only)

set(LCA_THIRD_PARTY_DIR "" CACHE PATH
    "Directory holding httplib.h, nlohmann/json.hpp and doctest/doctest.h for offline builds")

set(LCA_THIRD_PARTY_INCLUDE_DIR "${CMAKE_CURRENT_BINARY_DIR}/third_party/include")

function(lca_third_party_header relative_path url sha256)
  set(destination "${LCA_THIRD_PARTY_INCLUDE_DIR}/${relative_path}")
  if(EXISTS "${destination}")
    file(SHA256 "${destination}" existing)
    if(existing STREQUAL sha256)
      return()
    endif()
  endif()
  if(LCA_THIRD_PARTY_DIR)
    set(source "${LCA_THIRD_PARTY_DIR}/${relative_path}")
    if(NOT EXISTS "${source}")
      message(FATAL_ERROR "LCA_THIRD_PARTY_DIR has no ${relative_path}")
    endif()
    file(SHA256 "${source}" actual)
    if(NOT actual STREQUAL sha256)
      message(FATAL_ERROR "${source} has SHA-256 ${actual}; the pinned version is ${sha256}")
    endif()
    configure_file("${source}" "${destination}" COPYONLY)
  else()
    message(STATUS "lca-endpoint: downloading ${relative_path}")
    file(DOWNLOAD "${url}" "${destination}"
         EXPECTED_HASH SHA256=${sha256}
         TLS_VERIFY ON
         STATUS status)
    list(GET status 0 code)
    if(NOT code EQUAL 0)
      file(REMOVE "${destination}")
      message(FATAL_ERROR "download of ${url} failed: ${status}. "
                          "For offline builds set LCA_THIRD_PARTY_DIR.")
    endif()
  endif()
endfunction()

lca_third_party_header(
  httplib.h
  "https://raw.githubusercontent.com/yhirose/cpp-httplib/4f3f9ef19be83ae97a5d9a059432dc00e445b7ab/httplib.h"
  aa14e7e7bd2703694e0a6b6855af3b8c406102ab1fc56ac905fe33619b31faa5)

lca_third_party_header(
  nlohmann/json.hpp
  "https://raw.githubusercontent.com/nlohmann/json/55f93686c01528224f448c19128836e7df245f72/single_include/nlohmann/json.hpp"
  aaf127c04cb31c406e5b04a63f1ae89369fccde6d8fa7cdda1ed4f32dfc5de63)

if(LCA_ENDPOINT_TESTS)
  lca_third_party_header(
    doctest/doctest.h
    "https://raw.githubusercontent.com/doctest/doctest/2d0a9359a60c51affe2a9bebb1be1dca47868151/doctest/doctest.h"
    cfd518a3ef90f67e1f3ba514df23fb3627437de1a2feeba78cf5062a40021421)
endif()
