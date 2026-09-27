// lca-endpoint: a local OpenAI-compatible inference endpoint with pluggable
// backends. See internal/native_endpoint/README.md.

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <span>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "endpoint_server.hpp"
#include "fixture_backend.hpp"
#include "plugin_backend.hpp"
#if defined(LCA_WITH_OPENVINO_GENAI)
#include "openvino_genai_backend.hpp"
#endif

namespace {

constexpr int kExitOk = 0;
constexpr int kExitUsage = 2;
constexpr int kExitBackendFailed = 3;
constexpr int kExitBindFailed = 4;
constexpr int kExitSignalSetupFailed = 5;

// The one mutable global: a signal handler can reach nothing else safely.
std::atomic<bool> g_stop{false};  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

extern "C" void on_signal(int) { g_stop.store(true); }

const char* const kUsage =
    "usage: lca-endpoint --backend fixture|plugin|openvino-genai [options]\n"
    "\n"
    "backend selection:\n"
    "  --backend NAME              fixture, plugin or openvino-genai\n"
    "  --backend-config FILE       JSON configuration handed to the backend\n"
    "  --backend-config-json TEXT  the same, inline\n"
    "  --plugin PATH               shared library implementing lca/backend_plugin.h\n"
    "\n"
    "serving:\n"
    "  --host ADDRESS              default 127.0.0.1 (loopback only unless --allow-remote-bind)\n"
    "  --port N                    default 9000; 0 picks a free port\n"
    "  --allow-remote-bind         permit a non-loopback --host\n"
    "  --api-key-env NAME          require 'Authorization: Bearer $NAME' on every route but /health\n"
    "  --max-queued N              requests allowed to wait behind the running one (default 8)\n"
    "  --default-max-tokens N      when a request sets no max_tokens (default 2048)\n"
    "  --http-threads N            HTTP worker threads (default 8)\n"
    "  --quiet                     no per-request log lines on stderr\n"
    "\n"
    "  --version                   print the version and exit\n"
    "  --help                      print this text\n";

struct Arguments {
    std::string backend;
    std::string config_text;
    std::string plugin;
    lca::EndpointOptions options;
};

bool read_file(const std::string& path, std::string& out) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    std::ostringstream buffer;
    buffer << in.rdbuf();
    out = buffer.str();
    return true;
}

bool parse_positive(const std::string& text, long long lo, long long hi, long long& out) {
    try {
        std::size_t used = 0;
        out = std::stoll(text, &used);
        return used == text.size() && out >= lo && out <= hi;
    } catch (...) {
        return false;
    }
}

int parse_arguments(int argc, char** argv, Arguments& args) {
    args.options.port = 9000;
    if (argc < 1 || argv == nullptr) return kExitUsage;  // no program name: nothing to parse
    const std::span<char*> argv_span(argv, static_cast<std::size_t>(argc));
    std::vector<std::string> a(argv_span.begin() + 1, argv_span.end());
    for (std::size_t i = 0; i < a.size(); ++i) {
        const std::string& flag = a[i];
        auto value = [&](std::string& out) -> bool {
            if (i + 1 >= a.size()) {
                std::cerr << "lca-endpoint: " << flag << " needs a value\n";
                return false;
            }
            out = a[++i];
            return true;
        };
        auto number = [&](long long lo, long long hi, long long& out) -> bool {
            std::string text;
            if (!value(text)) return false;
            if (!parse_positive(text, lo, hi, out)) {
                std::cerr << "lca-endpoint: " << flag << " must be an integer in [" << lo << ", " << hi << "]\n";
                return false;
            }
            return true;
        };
        long long n = 0;
        if (flag == "--help" || flag == "-h") {
            std::cout << kUsage;
            return -1;
        } else if (flag == "--version") {
            std::cout << "lca-endpoint " << lca::kEndpointVersion << "\n";
            return -1;
        } else if (flag == "--backend") {
            if (!value(args.backend)) return kExitUsage;
        } else if (flag == "--backend-config") {
            std::string path;
            if (!value(path)) return kExitUsage;
            if (!read_file(path, args.config_text)) {
                std::cerr << "lca-endpoint: cannot read " << path << "\n";
                return kExitUsage;
            }
        } else if (flag == "--backend-config-json") {
            if (!value(args.config_text)) return kExitUsage;
        } else if (flag == "--plugin") {
            if (!value(args.plugin)) return kExitUsage;
        } else if (flag == "--host") {
            if (!value(args.options.host)) return kExitUsage;
        } else if (flag == "--port") {
            if (!number(0, 65535, n)) return kExitUsage;
            args.options.port = static_cast<int>(n);
        } else if (flag == "--allow-remote-bind") {
            args.options.allow_remote_bind = true;
        } else if (flag == "--api-key-env") {
            std::string name;
            if (!value(name)) return kExitUsage;
            const char* key = std::getenv(name.c_str());
            if (!key || !*key) {
                std::cerr << "lca-endpoint: environment variable " << name << " is empty or unset\n";
                return kExitUsage;
            }
            args.options.api_key = key;
        } else if (flag == "--max-queued") {
            if (!number(0, 1024, n)) return kExitUsage;
            args.options.scheduler.max_queued_requests = static_cast<std::size_t>(n);
        } else if (flag == "--default-max-tokens") {
            if (!number(1, 1 << 20, n)) return kExitUsage;
            args.options.scheduler.default_max_tokens = static_cast<std::uint32_t>(n);
        } else if (flag == "--http-threads") {
            if (!number(1, 256, n)) return kExitUsage;
            args.options.http_threads = static_cast<int>(n);
        } else if (flag == "--quiet") {
            args.options.log_requests = false;
        } else {
            std::cerr << "lca-endpoint: unknown argument " << flag << "\n\n" << kUsage;
            return kExitUsage;
        }
    }
    if (args.backend.empty()) {
        std::cerr << "lca-endpoint: --backend is required\n\n" << kUsage;
        return kExitUsage;
    }
    if (args.backend == "plugin" && args.plugin.empty()) {
        std::cerr << "lca-endpoint: --backend plugin needs --plugin PATH\n";
        return kExitUsage;
    }
    if (args.backend != "plugin" && !args.plugin.empty()) {
        std::cerr << "lca-endpoint: --plugin is only valid with --backend plugin\n";
        return kExitUsage;
    }
    if (args.backend != "fixture" && args.backend != "plugin" && args.backend != "openvino-genai") {
        std::cerr << "lca-endpoint: unknown backend " << args.backend << "\n";
        return kExitUsage;
    }
#if !defined(LCA_WITH_OPENVINO_GENAI)
    if (args.backend == "openvino-genai") {
        std::cerr << "lca-endpoint: this build does not include the openvino-genai backend "
                     "(configure with OpenVINOGenAI available)\n";
        return kExitUsage;
    }
#endif
    if (args.backend == "openvino-genai" && args.config_text.empty()) {
        std::cerr << "lca-endpoint: --backend openvino-genai needs --backend-config\n";
        return kExitUsage;
    }
    return kExitOk;
}

lca::BackendFactory make_factory(const Arguments& args) {
    const std::string backend = args.backend;
    const std::string config_text = args.config_text;
    const std::string plugin = args.plugin;
    return [backend, config_text, plugin]() -> std::unique_ptr<lca::InferenceBackend> {
        if (backend == "plugin") {
            return std::make_unique<lca::PluginBackend>(plugin, config_text.empty() ? "{}" : config_text);
        }
        nlohmann::json config = config_text.empty() ? nlohmann::json() : nlohmann::json::parse(config_text);
        if (backend == "fixture") {
            return std::make_unique<lca::FixtureBackend>(lca::FixtureConfig::from_json(config),
                                                         lca::kEndpointVersion);
        }
#if defined(LCA_WITH_OPENVINO_GENAI)
        return lca::make_openvino_genai_backend(lca::OpenVinoGenAiConfig::from_json(config));
#else
        throw std::runtime_error("openvino-genai backend is not built");
#endif
    };
}

}  // namespace

int main(int argc, char** argv) {
    Arguments args;
    const int parsed = parse_arguments(argc, argv, args);
    if (parsed == -1) return kExitOk;
    if (parsed != kExitOk) return parsed;

    if (std::signal(SIGINT, on_signal) == SIG_ERR || std::signal(SIGTERM, on_signal) == SIG_ERR) {
        // Without the handlers, Ctrl+C would kill the process without a clean stop.
        std::cerr << "lca-endpoint: cannot install signal handlers\n";
        return kExitSignalSetupFailed;
    }

    lca::EndpointServer server(args.options, make_factory(args));
    try {
        server.start();
    } catch (const std::exception& e) {
        std::cerr << "lca-endpoint: " << e.what() << "\n";
        return kExitBindFailed;
    }

    std::thread signal_watch([&server] {
        while (!g_stop.load()) std::this_thread::sleep_for(std::chrono::milliseconds(50));
        server.stop();
    });
    const bool healthy = server.wait();
    g_stop.store(true);
    signal_watch.join();
    if (!healthy) {
        std::cerr << "lca-endpoint: backend failed to load: " << server.load_error().value_or("") << "\n";
        return kExitBackendFailed;
    }
    return kExitOk;
}
