// Original software-only GenTL producer for Harvester development.
// MIT licensed; the unmodified GenTL header has its own license.
#include "GenTL_v1_6.h"
#include <nlohmann/json.hpp>
#include <dlfcn.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fstream>
#include <filesystem>
#include <limits>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace vfg {
using namespace GenTL;
using Clock = std::chrono::steady_clock;
using Lock = std::unique_lock<std::mutex>;
std::mutex mutex;
std::condition_variable changed;
thread_local GC_ERROR last_code = GC_ERR_SUCCESS;
thread_local std::string last_text = "Success";
struct Fault { GC_ERROR code; std::string text; };
[[noreturn]] void fail(GC_ERROR code, const std::string& text) { throw Fault{code, text}; }
void require(bool ok, GC_ERROR code = GC_ERR_INVALID_PARAMETER, const std::string& text = "Invalid parameter") {
    if (!ok) fail(code, text);
}
template<class T> void assign(T* output, T value) { require(output != nullptr); *output = value; }
void bytes(void* output, size_t* size, const void* data, size_t count) {
    require(size != nullptr);
    size_t capacity = *size;
    *size = count;
    if (!output) return;
    require(capacity >= count, GC_ERR_BUFFER_TOO_SMALL, "Output buffer too small");
    if (count) std::memcpy(output, data, count);
}
void strout(char* output, size_t* size, const std::string& value) { bytes(output, size, value.c_str(), value.size() + 1); }
struct Info {
    INFO_DATATYPE* type; void* output; size_t* size;
    template<class T> void value(INFO_DATATYPE t, T v) {
        if (type) *type = t;
        bytes(output, size, &v, sizeof(v));
    }
    void text(const std::string& v) { if (type) *type = INFO_DATATYPE_STRING; strout(static_cast<char*>(output), size, v); }
    void u64(uint64_t v) { value(INFO_DATATYPE_UINT64, v); }
    void u32(uint32_t v) { value(INFO_DATATYPE_UINT32, v); }
    void i32(int32_t v) { value(INFO_DATATYPE_INT32, v); }
    void sz(size_t v) { value(INFO_DATATYPE_SIZET, v); }
    void flag(bool v) { value(INFO_DATATYPE_BOOL8, bool8_t(v)); }
    void ptr(void* v) { value(INFO_DATATYPE_PTR, v); }
};
enum class Kind { System, Interface, Device, Remote, Stream, Buffer, Event };
struct CameraConfig {
    std::string id, serial, user_name, vendor, model;
};
struct BoardConfig {
    std::string id, display_name;
    std::vector<CameraConfig> cameras;
};
struct Topology {
    std::vector<BoardConfig> boards;
    std::string fingerprint;
    uint32_t columns = 1;
};

// Resolve relative to the loaded producer, never the consumer's working directory.
const std::filesystem::path& library_path() {
    static const auto path = [] {
        static int anchor;
        Dl_info info{};
        require(dladdr(&anchor, &info) && info.dli_fname, GC_ERR_IO, "Cannot locate VirtualFG.cti");
        return std::filesystem::canonical(info.dli_fname);
    }();
    return path;
}
std::string config_text(const nlohmann::json& object, const char* key,
                        const std::string& context, const std::string* fallback = nullptr) {
    auto it = object.find(key);
    if (it == object.end() && fallback) return *fallback;
    require(it != object.end() && it->is_string(), GC_ERR_INVALID_VALUE,
            context + "." + key + " must be a string");
    auto value = it->get<std::string>();
    require(!value.empty() && value.size() <= 255, GC_ERR_INVALID_VALUE,
            context + "." + key + " must contain 1..255 UTF-8 bytes");
    for (unsigned char ch : value)
        require(ch >= 32 && ch != 127, GC_ERR_INVALID_VALUE, context + "." + key + " contains a control character");
    return value;
}
void config_keys(const nlohmann::json& object, std::initializer_list<const char*> allowed,
                 const std::string& context) {
    require(object.is_object(), GC_ERR_INVALID_VALUE, context + " must be an object");
    for (auto it = object.begin(); it != object.end(); ++it)
        require(std::any_of(allowed.begin(), allowed.end(), [&](auto key) { return it.key() == key; }),
                GC_ERR_INVALID_VALUE, context + ": unknown field " + it.key());
}
std::shared_ptr<const Topology> load_topology() {
    const auto path = library_path().parent_path() / "virtualfg.json";
    std::ifstream input(path, std::ios::binary);
    require(bool(input), GC_ERR_IO, "Cannot open " + path.string());
    // The configuration contains at most 16 boards with 16 cameras each.
    std::string text;
    std::array<char, 4096> chunk{};
    while (input.read(chunk.data(), chunk.size()) || input.gcount()) {
        text.append(chunk.data(), size_t(input.gcount()));
        require(text.size() <= 1024 * 1024, GC_ERR_INVALID_VALUE, "virtualfg.json exceeds 1 MiB");
    }
    require(input.eof(), GC_ERR_IO, "Cannot read " + path.string());
    nlohmann::json doc;
    try {
        doc = nlohmann::json::parse(text, [](int depth, auto, auto&) {
            require(depth <= 16, GC_ERR_INVALID_VALUE, "virtualfg.json nesting is too deep");
            return true;
        });
    } catch (const nlohmann::json::exception& e) {
        fail(GC_ERR_INVALID_VALUE, "Invalid virtualfg.json: " + std::string(e.what()));
    }
    config_keys(doc, {"framegrabbers"}, "root");
    require(doc.contains("framegrabbers") && doc["framegrabbers"].is_array(),
            GC_ERR_INVALID_VALUE, "framegrabbers must be an array");
    require(doc["framegrabbers"].size() <= 16, GC_ERR_INVALID_VALUE, "At most 16 framegrabbers are supported");
    auto topology = std::make_shared<Topology>();
    std::unordered_set<std::string> board_ids, camera_ids, serials;
    for (const auto& entry : doc["framegrabbers"]) {
        config_keys(entry, {"id", "display_name", "cameras"}, "framegrabber");
        BoardConfig board;
        board.id = config_text(entry, "id", "framegrabber");
        require(board_ids.insert(board.id).second, GC_ERR_INVALID_VALUE, "Duplicate framegrabber ID: " + board.id);
        board.display_name = config_text(entry, "display_name", board.id, &board.id);
        require(entry.contains("cameras") && entry["cameras"].is_array(), GC_ERR_INVALID_VALUE,
                board.id + ".cameras must be an array");
        require(entry["cameras"].size() <= 16, GC_ERR_INVALID_VALUE, "At most 16 cameras per framegrabber are supported");
        for (const auto& item : entry["cameras"]) {
            config_keys(item, {"id", "serial_number", "user_defined_name", "vendor", "model"}, "camera");
            CameraConfig camera;
            camera.id = config_text(item, "id", board.id + ".camera");
            camera.serial = config_text(item, "serial_number", camera.id);
            require(camera_ids.insert(camera.id).second, GC_ERR_INVALID_VALUE, "Duplicate camera ID: " + camera.id);
            require(serials.insert(camera.serial).second, GC_ERR_INVALID_VALUE, "Duplicate camera serial: " + camera.serial);
            camera.user_name = config_text(item, "user_defined_name", camera.id, &camera.id);
            const std::string vendor = "VirtualFG", model = "VirtualMono8";
            camera.vendor = config_text(item, "vendor", camera.id, &vendor);
            camera.model = config_text(item, "model", camera.id, &model);
            board.cameras.push_back(std::move(camera));
        }
        topology->columns = std::max(topology->columns, uint32_t(board.cameras.size()));
        topology->boards.push_back(std::move(board));
    }
    topology->fingerprint = doc.dump();
    return topology;
}
std::string xml_escape(const std::string& value) {
    std::string out;
    for (char ch : value) {
        switch (ch) {
            case '&': out += "&amp;"; break;
            case '<': out += "&lt;"; break;
            case '>': out += "&gt;"; break;
            case '"': out += "&quot;"; break;
            case '\'': out += "&apos;"; break;
            default: out += ch;
        }
    }
    return out;
}
struct Scene {
    uint32_t width = 0, height = 0, columns = 4, rows = 3;
    uint32_t tile_width = 640, tile_height = 480;
    int step = 2;
    uint32_t circles = 2, circle_radius = 0;
    std::vector<uint8_t> pixels;
};
struct Object {
    Kind kind;
    void* handle = nullptr;
    void* parent = nullptr;
    void* remote = nullptr;
    void* event = nullptr;
    int board = 0, camera = 0;
    uint32_t interface_count = 0, camera_count = 0;
    std::string id, xml;
    std::shared_ptr<const Topology> topology;
    std::shared_ptr<Scene> scene;
    bool enumerated = false, active = false, running = false, locked = false, closed = false;
    uint32_t width = 640, height = 480, trigger = 0;
    double fps = 15.0, exposure = 1000.0;
    uint64_t triggers = 0, delivered = 0, remaining = 0, epoch = 0, timestamp = 0, frame = 0;
    uint64_t generated = 0, dropped = 0, underrun = 0;
    GC_ERROR failure = GC_ERR_SUCCESS;
    std::string failure_text = "Success";
    Clock::time_point next = Clock::now();
    std::vector<void*> buffers;
    std::deque<void*> queue;
    std::deque<void*> output;
    void* data = nullptr;
    void* user = nullptr;
    size_t capacity = 0, filled = 0;
    bool queued = false, fresh = false;
    std::vector<uint8_t> owned;
    explicit Object(Kind k) : kind(k) {}
};
std::unordered_map<void*, std::shared_ptr<Object>> objects;
uintptr_t next_handle = 1;
uint32_t initializations = 0;
constexpr uint64_t xml_address = 0x10000;
constexpr size_t max_image = 8192ULL * 8192;
constexpr uint32_t mono8 = 0x01080001;
std::thread engine_thread;
bool engine_stop = false, engine_closing = false;
void engine_main();
// This guard is destroyed before the thread, registry, condition variable and
// mutex. It also prevents a live worker from surviving a consumer's dlclose.
struct EngineShutdown {
    ~EngineShutdown() {
        { Lock lock(mutex); engine_stop = true; changed.notify_all(); }
        if (engine_thread.joinable()) engine_thread.join();
    }
} engine_shutdown;

std::shared_ptr<Object> get(void* h, Kind k) {
    auto it = objects.find(h);
    require(it != objects.end() && it->second->kind == k, GC_ERR_INVALID_HANDLE, "Invalid handle");
    return it->second;
}
std::shared_ptr<Object> port(void* h) {
    auto it = objects.find(h);
    require(it != objects.end() && it->second->kind != Kind::Buffer && it->second->kind != Kind::Event,
            GC_ERR_INVALID_HANDLE, "Invalid port handle");
    return it->second;
}
std::shared_ptr<Object> create(Kind kind, void* parent = nullptr) {
    auto o = std::make_shared<Object>(kind);
    o->handle = reinterpret_cast<void*>(next_handle++);
    o->parent = parent;
    objects.emplace(o->handle, o);
    return o;
}
void erase(void* h) { auto it = objects.find(h); if (it != objects.end()) { it->second->closed = true; objects.erase(it); } changed.notify_all(); }
bool has_children(void* h, Kind ignore = Kind::Remote) {
    for (const auto& entry : objects) if (entry.second->parent == h && entry.second->kind != ignore) return true;
    return false;
}
const CameraConfig& camera_config(const Object& o) { return o.topology->boards.at(o.board).cameras.at(o.camera); }
long env_integer(const char* key, long fallback, long minimum, long maximum) {
    const char* text = std::getenv(key);
    if (!text || !*text) return fallback;
    char* end = nullptr; long value = std::strtol(text, &end, 10);
    require(*end == '\0' && value >= minimum && value <= maximum, GC_ERR_INVALID_VALUE, "Invalid VFG environment setting");
    return value;
}
std::shared_ptr<Scene> load_scene(uint32_t rows, uint32_t columns) {
    auto scene = std::make_shared<Scene>(); scene->rows = rows; scene->columns = columns;
    scene->tile_width = uint32_t(env_integer("VFG_TILE_WIDTH", 640, 1, 8192));
    scene->tile_height = uint32_t(env_integer("VFG_TILE_HEIGHT", 480, 1, 8192));
    scene->step = int(env_integer("VFG_STEP_PIXELS", 2, -64, 64));
    scene->circles = uint32_t(env_integer("VFG_CIRCLE_COUNT", 2, 1, 2));
    scene->circle_radius = uint32_t(env_integer("VFG_CIRCLE_RADIUS", 0, 0, 8192));
    const char* path = std::getenv("VFG_SCENE_PGM");
    if (!path || !*path) return scene;
    std::ifstream input(path, std::ios::binary);
    require(bool(input), GC_ERR_IO, "Cannot open VFG_SCENE_PGM");
    auto token = [&]() {
        std::string value;
        for (;;) {
            input >> std::ws;
            if (input.peek() != '#') break;
            input.ignore(std::numeric_limits<std::streamsize>::max(), '\n');
        }
        input >> value;
        require(bool(input) && value.size() <= 16, GC_ERR_INVALID_VALUE, "Invalid PGM header");
        return value;
    };
    require(token() == "P5", GC_ERR_INVALID_VALUE, "Scene must be a binary 8-bit PGM (P5) image");
    auto dimension = [&]() {
        auto text = token(); char* end = nullptr; unsigned long n = std::strtoul(text.c_str(), &end, 10);
        require(*end == '\0' && n >= 1 && n <= 32768, GC_ERR_INVALID_VALUE, "Invalid PGM dimensions");
        return uint32_t(n);
    };
    scene->width = dimension(); scene->height = dimension();
    require(token() == "255", GC_ERR_INVALID_VALUE, "PGM maximum value must be 255");
    int separator = input.get();
    require(separator == '\n' || separator == '\r' || separator == ' ' || separator == '\t', GC_ERR_INVALID_VALUE, "Missing PGM separator");
    if (separator == '\r' && input.peek() == '\n') input.get();
    size_t count = size_t(scene->width) * scene->height;
    require(count <= 512ULL * 1024 * 1024, GC_ERR_RESOURCE_EXHAUSTED, "PGM scene exceeds 512 MiB");
    scene->pixels.resize(count);
    input.read(reinterpret_cast<char*>(scene->pixels.data()), std::streamsize(count));
    require(size_t(input.gcount()) == count, GC_ERR_IO, "PGM pixel data is truncated");
    return scene;
}
std::string module(Kind kind) {
    switch (kind) {
        case Kind::System: return "TLSystem";
        case Kind::Interface: return "TLInterface";
        case Kind::Device: return "TLDevice";
        case Kind::Remote: return "Device";
        case Kind::Stream: return "TLDataStream";
        default: return "Unknown";
    }
}
std::string reg(const std::string& name, uint32_t address, bool floating = false, const char* access = "RW") {
    std::string tag = floating ? "FloatReg" : "IntReg";
    return "<" + tag + " Name=\"" + name + "Reg\"><Address>" + std::to_string(address) +
        "</Address><Length>" + (floating ? "8" : "4") + "</Length><AccessMode>" + access +
        "</AccessMode><pPort>Device</pPort><Cachable>NoCache</Cachable>" +
        (floating ? "" : "<Sign>Unsigned</Sign>") + "<Endianess>LittleEndian</Endianess></" + tag + ">";
}
std::string xml(Object& o) {
    std::string x = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<RegisterDescription ModelName=\"VirtualFG\" VendorName=\"VirtualFG\""
        " StandardNameSpace=\"None\" SchemaMajorVersion=\"1\" SchemaMinorVersion=\"1\" SchemaSubMinorVersion=\"0\""
        " MajorVersion=\"1\" MinorVersion=\"1\" SubMinorVersion=\"0\""
        " ProductGuid=\"A2BA40AB-6A73-49FE-9DF8-EF6B61F02F00\" VersionGuid=\"63CD6848-C23D-42F3-9DAA-80E3BD33C5D1\""
        " xmlns=\"http://www.genicam.org/GenApi/Version_1_1\">";
    if (o.kind != Kind::Remote) {
        x += "<Category Name=\"Root\"><pFeature>ModuleName</pFeature></Category>"
             "<String Name=\"ModuleName\"><Value>" + module(o.kind) + "</Value></String>";
    } else {
        std::vector<std::string> features = {"DeviceVendorName", "DeviceModelName", "DeviceSerialNumber", "DeviceUserID", "Width", "Height",
            "WidthMax", "HeightMax", "OffsetX", "OffsetY", "PixelFormat", "PayloadSize", "AcquisitionMode", "AcquisitionFrameRate",
            "ExposureTime", "TriggerSelector", "TriggerMode", "TriggerSource", "TriggerSoftware", "AcquisitionStart", "AcquisitionStop", "TLParamsLocked",
            "TileColumn", "TileRow", "MosaicColumns", "MosaicRows", "MotionStepPixels", "FrameCounter", "DroppedFrameCount"};
        x += "<Category Name=\"Root\">";
        for (auto& f : features) x += "<pFeature>" + f + "</pFeature>";
        x += "</Category>";
        auto string_node = [&](const std::string& name, const std::string& value) {
            x += "<String Name=\"" + name + "\"><Value>" + xml_escape(value) + "</Value></String>";
        };
        const auto& camera = camera_config(o);
        string_node("DeviceVendorName", camera.vendor);
        string_node("DeviceModelName", camera.model);
        string_node("DeviceSerialNumber", camera.serial);
        string_node("DeviceUserID", camera.user_name);
        for (auto& entry : std::vector<std::pair<std::string, int>>{{"TileColumn", o.camera}, {"TileRow", o.board},
            {"MosaicColumns", int(o.scene->columns)}, {"MosaicRows", int(o.scene->rows)}, {"MotionStepPixels", o.scene->step}}) {
            x += "<Integer Name=\"" + entry.first + "\"><Value>" + std::to_string(entry.second) + "</Value></Integer>";
        }
        auto integer = [&](const std::string& name, uint32_t address, uint32_t min, uint32_t max) {
            x += "<Integer Name=\"" + name + "\"><pValue>" + name + "Reg</pValue><Min>" + std::to_string(min) + "</Min><Max>" +
                std::to_string(max) + "</Max><Inc>1</Inc></Integer>" + reg(name, address);
        };
        integer("Width", 0, 1, 8192); integer("Height", 4, 1, 8192); integer("TLParamsLocked", 0x4c, 0, 1);
        for (auto& entry : std::vector<std::pair<std::string, uint32_t>>{{"FrameCounter", 0x50}, {"DroppedFrameCount", 0x58}}) {
            x += "<Integer Name=\"" + entry.first + "\"><pValue>" + entry.first + "Reg</pValue></Integer>"
                 "<IntReg Name=\"" + entry.first + "Reg\"><Address>" + std::to_string(entry.second) +
                 "</Address><Length>8</Length><AccessMode>RO</AccessMode><pPort>Device</pPort>"
                 "<Cachable>NoCache</Cachable><Sign>Unsigned</Sign><Endianess>LittleEndian</Endianess></IntReg>";
        }
        x += "<Integer Name=\"WidthMax\"><Value>8192</Value></Integer><Integer Name=\"HeightMax\"><Value>8192</Value></Integer>"
             "<Integer Name=\"OffsetX\"><Value>0</Value></Integer><Integer Name=\"OffsetY\"><Value>0</Value></Integer>"
             "<IntSwissKnife Name=\"PayloadSize\"><pVariable Name=\"W\">Width</pVariable><pVariable Name=\"H\">Height</pVariable><Formula>W*H</Formula></IntSwissKnife>";
        auto enumeration = [&](const std::string& name, uint32_t address, const std::vector<std::pair<std::string, int>>& entries) {
            x += "<Enumeration Name=\"" + name + "\">";
            for (auto& entry : entries) x += "<EnumEntry Name=\"" + entry.first + "\"><Value>" + std::to_string(entry.second) + "</Value></EnumEntry>";
            x += "<pValue>" + name + "Reg</pValue></Enumeration>" + reg(name, address);
        };
        enumeration("PixelFormat", 8, {{"Mono8", mono8}});
        enumeration("AcquisitionMode", 12, {{"Continuous", 1}});
        enumeration("TriggerMode", 0x30, {{"Off", 0}, {"On", 1}});
        enumeration("TriggerSource", 0x34, {{"Software", 0}});
        enumeration("TriggerSelector", 0x38, {{"FrameStart", 0}});
        x += "<Float Name=\"AcquisitionFrameRate\"><pValue>AcquisitionFrameRateReg</pValue><Min>0.1</Min><Max>1000.0</Max><Unit>Hz</Unit></Float>" + reg("AcquisitionFrameRate", 0x20, true);
        x += "<Float Name=\"ExposureTime\"><pValue>ExposureTimeReg</pValue><Min>1.0</Min><Max>1000000.0</Max><Unit>us</Unit></Float>" + reg("ExposureTime", 0x28, true);
        for (auto& entry : std::vector<std::pair<std::string, uint32_t>>{{"AcquisitionStart", 0x40}, {"AcquisitionStop", 0x44}, {"TriggerSoftware", 0x48}}) {
            x += "<Command Name=\"" + entry.first + "\"><pValue>" + entry.first + "Reg</pValue><CommandValue>1</CommandValue></Command>" + reg(entry.first, entry.second);
        }
    }
    return x + "<Port Name=\"Device\"/></RegisterDescription>";
}
void set_xml(const std::shared_ptr<Object>& o) { o->xml = xml(*o); }
std::string url(Object& o) {
    std::ostringstream s;
    // IDs may contain punctuation or Unicode; never put them in a local URL.
    s << "local:VirtualFG_v05_" << module(o.kind) << "_" << uintptr_t(o.handle) << ".xml;" << std::hex << xml_address << ";" << o.xml.size();
    return s.str();
}
void tl_info(int cmd, Info info) {
    switch (cmd) {
        case TL_INFO_ID: info.text("VirtualFG"); break;
        case TL_INFO_VENDOR: info.text("VirtualFG"); break;
        case TL_INFO_MODEL: info.text("SoftwareGenTL"); break;
        case TL_INFO_VERSION: info.text("0.5.0"); break;
        case TL_INFO_TLTYPE: info.text("Custom"); break;
        case TL_INFO_NAME: info.text("VirtualFG.cti"); break;
        case TL_INFO_PATHNAME: info.text(library_path().string()); break;
        case TL_INFO_DISPLAYNAME: info.text("Virtual framegrabbers"); break;
        case TL_INFO_CHAR_ENCODING: info.i32(TL_CHAR_ENCODING_UTF8); break;
        case TL_INFO_GENTL_VER_MAJOR: info.u32(1); break;
        case TL_INFO_GENTL_VER_MINOR: info.u32(5); break;
        default: fail(GC_ERR_NOT_IMPLEMENTED, "Unsupported TL information");
    }
}
void interface_info(const BoardConfig& board, int cmd, Info info) {
    switch (cmd) {
        case INTERFACE_INFO_ID: info.text(board.id); break;
        case INTERFACE_INFO_DISPLAYNAME: info.text(board.display_name); break;
        case INTERFACE_INFO_TLTYPE: info.text("Custom"); break;
        default: fail(GC_ERR_NOT_IMPLEMENTED, "Unsupported interface information");
    }
}
void device_info(const CameraConfig& camera, int cmd, Info info) {
    switch (cmd) {
        case DEVICE_INFO_ID: info.text(camera.id); break;
        case DEVICE_INFO_SERIAL_NUMBER: info.text(camera.serial); break;
        case DEVICE_INFO_USER_DEFINED_NAME: info.text(camera.user_name); break;
        case DEVICE_INFO_VENDOR: info.text(camera.vendor); break;
        case DEVICE_INFO_MODEL: info.text(camera.model); break;
        case DEVICE_INFO_TLTYPE: info.text("Custom"); break;
        case DEVICE_INFO_DISPLAYNAME: info.text(camera.user_name); break;
        case DEVICE_INFO_ACCESS_STATUS: {
            int32_t status = DEVICE_ACCESS_STATUS_READWRITE;
            for (auto& e : objects) if (e.second->kind == Kind::Device && camera_config(*e.second).serial == camera.serial) status = DEVICE_ACCESS_STATUS_OPEN_READWRITE;
            info.i32(status); break;
        }
        case DEVICE_INFO_VERSION: info.text("0.5.0"); break;
        case DEVICE_INFO_TIMESTAMP_FREQUENCY: info.u64(1000000000); break;
        default: fail(GC_ERR_NOT_IMPLEMENTED, "Unsupported device information");
    }
}
int find_board(Object& tl, const char* id) {
    require(id != nullptr);
    for (uint32_t i = 0; i < tl.interface_count; ++i) if (tl.topology->boards[i].id == id) return i;
    fail(GC_ERR_INVALID_ID, "Unknown interface ID");
}
int find_camera(Object& iface, const char* id) {
    require(id != nullptr);
    for (uint32_t i = 0; i < iface.camera_count; ++i) if (iface.topology->boards[iface.board].cameras[i].id == id) return i;
    fail(GC_ERR_INVALID_ID, "Unknown camera ID");
}
#define INFO_ARGS INFO_DATATYPE* type, void* output, size_t* size
#define INFO Info{type, output, size}
void impl_GCInitLib(Lock&) {
    require(!engine_closing, GC_ERR_BUSY, "Producer is shutting down");
    if (!initializations) { engine_stop = false; engine_thread = std::thread(engine_main); }
    ++initializations;
}
void impl_GCCloseLib(Lock& lock) {
    require(initializations, GC_ERR_NOT_INITIALIZED, "Library not initialized");
    require(initializations > 1 || objects.empty(), GC_ERR_RESOURCE_IN_USE, "Close all handles before closing library");
    if (--initializations == 0) {
        engine_closing = true; engine_stop = true; changed.notify_all();
        lock.unlock();
        if (engine_thread.joinable()) engine_thread.join();
        lock.lock(); engine_closing = false;
    }
}
void impl_GCGetInfo(Lock&, TL_INFO_CMD cmd, INFO_ARGS) { tl_info(cmd, INFO); }
void impl_GCGetLastError(Lock&, GC_ERROR* code, char* output, size_t* size) { assign(code, last_code); strout(output, size, last_text); }
void impl_TLOpen(Lock&, TL_HANDLE* handle) {
    require(handle != nullptr);
    require(initializations, GC_ERR_NOT_INITIALIZED, "Library not initialized");
    auto topology = load_topology();
    auto boards = uint32_t(topology->boards.size()), cameras = topology->columns;
    auto scene = load_scene(std::max(boards, uint32_t(1)), cameras);
    auto o = create(Kind::System);
    try {
        o->id = "VirtualFG"; o->interface_count = boards; o->topology = topology;
        o->scene = scene; set_xml(o); *handle = o->handle;
    } catch (...) { erase(o->handle); throw; }
}
void impl_TLClose(Lock&, TL_HANDLE h) { get(h, Kind::System); require(!has_children(h), GC_ERR_RESOURCE_IN_USE, "Interfaces remain open"); erase(h); }
void impl_TLGetInfo(Lock&, TL_HANDLE h, TL_INFO_CMD cmd, INFO_ARGS) { get(h, Kind::System); tl_info(cmd, INFO); }
void impl_TLGetNumInterfaces(Lock&, TL_HANDLE h, uint32_t* count) { assign(count, get(h, Kind::System)->interface_count); }
void impl_TLGetInterfaceID(Lock&, TL_HANDLE h, uint32_t index, char* output, size_t* size) {
    auto o = get(h, Kind::System);
    require(index < o->interface_count, GC_ERR_INVALID_INDEX); strout(output, size, o->topology->boards[index].id);
}
void impl_TLGetInterfaceInfo(Lock&, TL_HANDLE h, const char* id, INTERFACE_INFO_CMD cmd, INFO_ARGS) {
    auto o = get(h, Kind::System); interface_info(o->topology->boards[find_board(*o, id)], cmd, INFO);
}
void impl_TLUpdateInterfaceList(Lock&, TL_HANDLE h, bool8_t* updated, uint64_t) {
    auto o = get(h, Kind::System);
    auto topology = load_topology();
    bool different = topology->fingerprint != o->topology->fingerprint;
    if (different) {
        // Harvester.update() closes acquisitions/interfaces before rediscovery.
        // Direct GenTL users must do the same before replacing an active tree.
        require(!has_children(h), GC_ERR_RESOURCE_IN_USE, "Close interfaces before reloading virtualfg.json");
        auto scene = load_scene(std::max(uint32_t(topology->boards.size()), uint32_t(1)), topology->columns);
        o->topology = topology; o->scene = scene; o->interface_count = uint32_t(topology->boards.size());
    }
    if (updated) *updated = different || !o->enumerated;
    o->enumerated = true;
}
void impl_TLOpenInterface(Lock&, TL_HANDLE h, const char* id, IF_HANDLE* handle) {
    require(handle != nullptr); auto tl = get(h, Kind::System); int board = find_board(*tl, id);
    auto o = create(Kind::Interface, h);
    try {
        o->board = board; o->topology = tl->topology;
        o->camera_count = uint32_t(o->topology->boards[board].cameras.size());
        o->scene = tl->scene; o->id = id; set_xml(o); *handle = o->handle;
    } catch (...) { erase(o->handle); throw; }
}
void impl_IFClose(Lock&, IF_HANDLE h) { get(h, Kind::Interface); require(!has_children(h), GC_ERR_RESOURCE_IN_USE, "Devices remain open"); erase(h); }
void impl_IFGetInfo(Lock&, IF_HANDLE h, INTERFACE_INFO_CMD cmd, INFO_ARGS) {
    auto o = get(h, Kind::Interface); interface_info(o->topology->boards[o->board], cmd, INFO);
}
void impl_IFGetNumDevices(Lock&, IF_HANDLE h, uint32_t* count) { assign(count, get(h, Kind::Interface)->camera_count); }
void impl_IFGetDeviceID(Lock&, IF_HANDLE h, uint32_t index, char* output, size_t* size) {
    auto o = get(h, Kind::Interface); require(index < o->camera_count, GC_ERR_INVALID_INDEX);
    strout(output, size, o->topology->boards[o->board].cameras[index].id);
}
void impl_IFUpdateDeviceList(Lock&, IF_HANDLE h, bool8_t* updated, uint64_t) { auto o = get(h, Kind::Interface); if (updated) *updated = !o->enumerated; o->enumerated = true; }
void impl_IFGetDeviceInfo(Lock&, IF_HANDLE h, const char* id, DEVICE_INFO_CMD cmd, INFO_ARGS) {
    auto o = get(h, Kind::Interface); device_info(o->topology->boards[o->board].cameras[find_camera(*o, id)], cmd, INFO);
}
void impl_IFOpenDevice(Lock&, IF_HANDLE h, const char* id, DEVICE_ACCESS_FLAGS flags, DEV_HANDLE* handle) {
    require(handle != nullptr);
    require(flags == DEVICE_ACCESS_CONTROL || flags == DEVICE_ACCESS_EXCLUSIVE, GC_ERR_NOT_IMPLEMENTED, "Only control and exclusive opens are supported");
    auto iface = get(h, Kind::Interface); int camera = find_camera(*iface, id);
    const auto& config = iface->topology->boards[iface->board].cameras[camera];
    for (auto& e : objects) require(e.second->kind != Kind::Device || camera_config(*e.second).serial != config.serial,
            GC_ERR_RESOURCE_IN_USE, "Camera already open in this process");
    auto o = create(Kind::Device, h);
    try {
        o->board = iface->board; o->camera = camera; o->topology = iface->topology;
        o->scene = iface->scene; o->id = id; set_xml(o);
        auto r = create(Kind::Remote, o->handle); o->remote = r->handle;
        r->board = o->board; r->camera = camera; r->scene = o->scene; r->topology = o->topology;
        r->width = r->scene->tile_width; r->height = r->scene->tile_height;
        r->id = id; set_xml(r); *handle = o->handle;
    } catch (...) { erase(o->remote); erase(o->handle); throw; }
}
void impl_DevGetPort(Lock&, DEV_HANDLE h, PORT_HANDLE* handle) { assign(handle, get(h, Kind::Device)->remote); }
void impl_DevGetNumDataStreams(Lock&, DEV_HANDLE h, uint32_t* count) { get(h, Kind::Device); assign(count, uint32_t(1)); }
void impl_DevGetDataStreamID(Lock&, DEV_HANDLE h, uint32_t index, char* output, size_t* size) { get(h, Kind::Device); require(index == 0, GC_ERR_INVALID_INDEX); strout(output, size, "Stream0"); }
void impl_DevOpenDataStream(Lock&, DEV_HANDLE h, const char* id, DS_HANDLE* handle) {
    require(handle != nullptr && id != nullptr); auto d = get(h, Kind::Device); require(std::string(id) == "Stream0", GC_ERR_INVALID_ID);
    require(!has_children(h), GC_ERR_RESOURCE_IN_USE, "Stream already open");
    auto o = create(Kind::Stream, h);
    try {
        o->remote = d->remote; o->id = id; set_xml(o); *handle = o->handle;
    } catch (...) { erase(o->handle); throw; }
}
void impl_DevGetInfo(Lock&, DEV_HANDLE h, DEVICE_INFO_CMD cmd, INFO_ARGS) { auto o = get(h, Kind::Device); device_info(camera_config(*o), cmd, INFO); }
void impl_DevClose(Lock&, DEV_HANDLE h) { auto d = get(h, Kind::Device); require(!has_children(h), GC_ERR_RESOURCE_IN_USE, "Stream remains open"); erase(d->remote); erase(h); }
void impl_IFGetParentTL(Lock&, IF_HANDLE h, TL_HANDLE* parent) { assign(parent, get(h, Kind::Interface)->parent); }
void impl_DevGetParentIF(Lock&, DEV_HANDLE h, IF_HANDLE* parent) { assign(parent, get(h, Kind::Device)->parent); }
void impl_DSGetParentDev(Lock&, DS_HANDLE h, DEV_HANDLE* parent) { assign(parent, get(h, Kind::Stream)->parent); }

void impl_GCGetPortURL(Lock&, PORT_HANDLE h, char* output, size_t* size) { strout(output, size, url(*port(h))); }
void impl_GCGetNumPortURLs(Lock&, PORT_HANDLE h, uint32_t* count) { port(h); assign(count, uint32_t(1)); }
void impl_GCGetPortURLInfo(Lock&, PORT_HANDLE h, uint32_t index, URL_INFO_CMD cmd, INFO_ARGS) {
    auto o = port(h); require(index == 0, GC_ERR_INVALID_INDEX); auto info = INFO;
    switch (cmd) {
        case URL_INFO_URL: info.text(url(*o)); break;
        case URL_INFO_SCHEMA_VER_MAJOR: case URL_INFO_SCHEMA_VER_MINOR: case URL_INFO_FILE_VER_MAJOR: case URL_INFO_FILE_VER_MINOR: info.i32(1); break;
        case URL_INFO_FILE_VER_SUBMINOR: case URL_INFO_SCHEME: info.i32(0); break;
        case URL_INFO_FILE_REGISTER_ADDRESS: info.u64(xml_address); break;
        case URL_INFO_FILE_SIZE: info.u64(o->xml.size()); break;
        default: fail(GC_ERR_NOT_IMPLEMENTED, "Unsupported URL information");
    }
}
void impl_GCGetPortInfo(Lock&, PORT_HANDLE h, PORT_INFO_CMD cmd, INFO_ARGS) {
    auto o = port(h); auto info = INFO;
    switch (cmd) {
        case PORT_INFO_ID: info.text(o->id); break;
        case PORT_INFO_VENDOR: case PORT_INFO_MODEL: info.text("VirtualFG"); break;
        case PORT_INFO_TLTYPE: info.text("Custom"); break;
        case PORT_INFO_MODULE: info.text(module(o->kind)); break;
        case PORT_INFO_LITTLE_ENDIAN: case PORT_INFO_ACCESS_READ: info.flag(true); break;
        case PORT_INFO_ACCESS_WRITE: info.flag(o->kind == Kind::Remote); break;
        case PORT_INFO_BIG_ENDIAN: case PORT_INFO_ACCESS_NA: case PORT_INFO_ACCESS_NI: info.flag(false); break;
        case PORT_INFO_VERSION: info.text("0.5.0"); break;
        case PORT_INFO_PORTNAME: info.text("Device"); break;
        default: fail(GC_ERR_NOT_IMPLEMENTED, "Unsupported port information");
    }
}
void impl_GCReadPort(Lock&, PORT_HANDLE h, uint64_t address, void* output, size_t* size) {
    auto o = port(h); require(size && output);
    if (address >= xml_address) {
        uint64_t offset = address - xml_address;
        require(offset <= o->xml.size() && *size <= o->xml.size() - offset, GC_ERR_INVALID_ADDRESS, "XML read outside address range");
        std::memcpy(output, o->xml.data() + offset, *size); return;
    }
    require(o->kind == Kind::Remote, GC_ERR_INVALID_ADDRESS, "Module has no register at this address");
    std::array<uint8_t, 0x60> regs{};
    auto put = [&](size_t pos, auto value) { std::memcpy(regs.data() + pos, &value, sizeof(value)); };
    put(0, o->width); put(4, o->height); put(8, mono8); put(12, uint32_t(1));
    put(0x20, o->fps); put(0x28, o->exposure); put(0x30, o->trigger); put(0x4c, uint32_t(o->locked));
    put(0x50, o->frame); put(0x58, o->dropped);
    require(address <= regs.size() && *size <= regs.size() - address, GC_ERR_INVALID_ADDRESS, "Register read outside address range");
    std::memcpy(output, regs.data() + address, *size);
}
void impl_GCWritePort(Lock&, PORT_HANDLE h, uint64_t address, const void* input, size_t* size) {
    auto o = get(h, Kind::Remote); require(size && input);
    bool stream_running = false;
    for (auto& e : objects) if (e.second->kind == Kind::Stream && e.second->remote == h && e.second->running) stream_running = true;
    bool configuration = address < 0x40;
    require(!configuration || (!o->locked && !stream_running && !o->active), GC_ERR_ACCESS_DENIED, "Stop acquisition before changing camera configuration");
    if (address == 0x20 || address == 0x28) {
        require(*size == 8); double value; std::memcpy(&value, input, 8);
        require(std::isfinite(value) && value >= (address == 0x20 ? 0.1 : 1.0) && value <= (address == 0x20 ? 1000.0 : 1000000.0), GC_ERR_INVALID_VALUE);
        if (address == 0x20) o->fps = value; else o->exposure = value;
    } else {
        require(*size == 4); uint32_t value; std::memcpy(&value, input, 4);
        switch (address) {
            case 0: case 4: {
                require(value >= 1 && value <= 8192, GC_ERR_INVALID_VALUE);
                auto& dimension = address == 0 ? o->width : o->height;
                dimension = value;
                break;
            }
            case 8: require(value == mono8, GC_ERR_INVALID_VALUE); break;
            case 12: require(value == 1, GC_ERR_INVALID_VALUE); break;
            case 0x30: require(value <= 1, GC_ERR_INVALID_VALUE); o->trigger = value; o->triggers = 0; break;
            case 0x34: case 0x38: require(value == 0, GC_ERR_INVALID_VALUE); break;
            case 0x40:
                require(value == 1, GC_ERR_INVALID_VALUE); o->active = true; o->triggers = 0;
                for (auto& e : objects) if (e.second->kind == Kind::Stream && e.second->remote == h) e.second->next = Clock::now();
                break;
            case 0x44: require(value == 1, GC_ERR_INVALID_VALUE); o->active = false; o->triggers = 0; break;
            case 0x48:
                require(value == 1 && o->trigger == 1 && o->active && stream_running, GC_ERR_NOT_AVAILABLE, "Software trigger requires active acquisition and TriggerMode On");
                require(o->triggers < 1024, GC_ERR_RESOURCE_EXHAUSTED, "Software trigger queue full"); ++o->triggers; break;
            case 0x4c: require(value <= 1, GC_ERR_INVALID_VALUE); o->locked = value != 0; break;
            default: fail(GC_ERR_INVALID_ADDRESS, "Unknown writable register");
        }
    }
    changed.notify_all();
}
void impl_GCReadPortStacked(Lock& lock, PORT_HANDLE h, PORT_REGISTER_STACK_ENTRY* entries, size_t* count) {
    require(count && (entries || !*count)); size_t n = *count; *count = 0;
    for (size_t i = 0; i < n; ++i) { size_t size = entries[i].Size; impl_GCReadPort(lock, h, entries[i].Address, entries[i].pBuffer, &size); ++*count; }
}
void impl_GCWritePortStacked(Lock& lock, PORT_HANDLE h, PORT_REGISTER_STACK_ENTRY* entries, size_t* count) {
    require(count && (entries || !*count)); size_t n = *count; *count = 0;
    for (size_t i = 0; i < n; ++i) { size_t size = entries[i].Size; impl_GCWritePort(lock, h, entries[i].Address, entries[i].pBuffer, &size); ++*count; }
}

std::shared_ptr<Object> buffer(void* stream, void* h) { get(stream, Kind::Stream); auto b = get(h, Kind::Buffer); require(b->parent == stream, GC_ERR_INVALID_HANDLE, "Buffer belongs to a different stream"); return b; }
void announce(void* h, void* data, size_t size, void* user, void** handle, bool allocate) {
    require(handle && (allocate || data)); auto s = get(h, Kind::Stream);
    require(size > 0 && size <= max_image, GC_ERR_INVALID_VALUE, "Buffer size must be from 1 to 64 MiB");
    require(s->buffers.size() < 64, GC_ERR_RESOURCE_EXHAUSTED, "Maximum of 64 buffers per stream");
    auto b = create(Kind::Buffer, h);
    try {
        b->capacity = size; b->user = user;
        if (allocate) { b->owned.resize(size); b->data = b->owned.data(); } else b->data = data;
        s->buffers.push_back(b->handle); *handle = b->handle;
    } catch (...) {
        erase(b->handle); throw;  // Allocation failure must not leave an orphan handle.
    }
}
void impl_DSAnnounceBuffer(Lock&, DS_HANDLE h, void* data, size_t size, void* user, BUFFER_HANDLE* handle) { announce(h, data, size, user, handle, false); }
void impl_DSAllocAndAnnounceBuffer(Lock&, DS_HANDLE h, size_t size, void* user, BUFFER_HANDLE* handle) { announce(h, nullptr, size, user, handle, true); }
void impl_DSQueueBuffer(Lock&, DS_HANDLE h, BUFFER_HANDLE bh) {
    auto s = get(h, Kind::Stream); auto b = buffer(h, bh); require(!b->queued, GC_ERR_RESOURCE_IN_USE, "Buffer already queued");
    s->queue.push_back(bh); b->queued = true; b->fresh = false; b->filled = 0; changed.notify_all();
}
void impl_DSRevokeBuffer(Lock&, DS_HANDLE h, BUFFER_HANDLE bh, void** data, void** user) {
    auto s = get(h, Kind::Stream); auto b = buffer(h, bh); require(!s->running && !b->queued, GC_ERR_RESOURCE_IN_USE, "Stop and flush stream before revoking buffers");
    if (data) *data = b->data;
    if (user) *user = b->user;
    s->buffers.erase(std::remove(s->buffers.begin(), s->buffers.end(), bh), s->buffers.end()); erase(bh);
}
void impl_DSGetBufferID(Lock&, DS_HANDLE h, uint32_t index, BUFFER_HANDLE* handle) { auto s = get(h, Kind::Stream); require(index < s->buffers.size(), GC_ERR_INVALID_INDEX); assign(handle, s->buffers[index]); }
void impl_DSFlushQueue(Lock&, DS_HANDLE h, ACQ_QUEUE_TYPE operation) {
    auto s = get(h, Kind::Stream);
    require(!s->running, GC_ERR_RESOURCE_IN_USE, "Stop stream before flushing buffers");
    require(operation >= 1 && operation <= 4, GC_ERR_NOT_IMPLEMENTED, "Input-to-output flush is unsupported");
    if (operation == ACQ_QUEUE_OUTPUT_DISCARD) {
        for (auto bh : s->output) get(bh, Kind::Buffer)->queued = false;
        s->output.clear(); return;
    }
    if (operation != ACQ_QUEUE_UNQUEUED_TO_INPUT) { s->queue.clear(); s->output.clear(); for (auto bh : s->buffers) get(bh, Kind::Buffer)->queued = false; }
    if (operation == ACQ_QUEUE_ALL_DISCARD) return;
    for (auto bh : s->buffers) { auto b = get(bh, Kind::Buffer); if (!b->queued) { s->queue.push_back(bh); b->queued = true; b->fresh = false; b->filled = 0; } }
}
void impl_DSStartAcquisition(Lock&, DS_HANDLE h, ACQ_START_FLAGS flags, uint64_t count) {
    auto s = get(h, Kind::Stream); auto d = get(s->remote, Kind::Remote);
    require(flags == ACQ_START_FLAGS_DEFAULT && count > 0); require(!s->running, GC_ERR_RESOURCE_IN_USE, "Stream already started");
    require(s->queue.size() >= 3, GC_ERR_RESOURCE_IN_USE, "Queue at least three buffers before starting");
    for (auto bh : s->buffers) require(get(bh, Kind::Buffer)->capacity >= size_t(d->width) * d->height, GC_ERR_BUFFER_TOO_SMALL, "Announced buffer is smaller than image payload");
    require(s->output.empty(), GC_ERR_RESOURCE_IN_USE, "Flush old output buffers before restarting");
    s->running = true; s->remaining = count; s->delivered = 0; s->generated = 0; s->dropped = 0; s->underrun = 0; s->frame = 0;
    s->failure = GC_ERR_SUCCESS; s->failure_text = "Success";
    s->next = Clock::now(); d->triggers = 0; d->frame = 0; d->dropped = 0; changed.notify_all();
}
void impl_DSStopAcquisition(Lock&, DS_HANDLE h, ACQ_STOP_FLAGS flags) { require(flags == 0 || flags == 1); auto s = get(h, Kind::Stream); s->running = false; ++s->epoch; changed.notify_all(); }
void impl_DSClose(Lock&, DS_HANDLE h) {
    auto s = get(h, Kind::Stream); require(!s->running && s->buffers.empty(), GC_ERR_RESOURCE_IN_USE, "Stop stream and revoke buffers before closing");
    std::vector<void*> events; for (auto& e : objects) if (e.second->parent == h && e.second->kind == Kind::Event) events.push_back(e.first);
    for (auto e : events) erase(e);
    erase(h);
}
void impl_DSGetInfo(Lock&, DS_HANDLE h, STREAM_INFO_CMD cmd, INFO_ARGS) {
    auto s = get(h, Kind::Stream); auto d = get(s->remote, Kind::Remote); auto info = INFO;
    switch (cmd) {
        case STREAM_INFO_ID: info.text(s->id); break;
        case STREAM_INFO_NUM_DELIVERED: info.u64(s->delivered); break;
        case STREAM_INFO_NUM_STARTED: info.u64(s->generated); break;
        case STREAM_INFO_NUM_UNDERRUN: info.u64(s->underrun); break;
        case STREAM_INFO_NUM_ANNOUNCED: info.sz(s->buffers.size()); break;
        case STREAM_INFO_NUM_QUEUED: info.sz(s->queue.size()); break;
        case STREAM_INFO_NUM_AWAIT_DELIVERY: info.sz(s->output.size()); break;
        case STREAM_INFO_NUM_CHUNKS_MAX: info.sz(0); break;
        case STREAM_INFO_PAYLOAD_SIZE: info.sz(size_t(d->width) * d->height); break;
        case STREAM_INFO_IS_GRABBING: info.flag(s->running); break;
        case STREAM_INFO_DEFINES_PAYLOADSIZE: info.flag(true); break;
        case STREAM_INFO_TLTYPE: info.text("Custom"); break;
        case STREAM_INFO_BUF_ANNOUNCE_MIN: info.sz(3); break;
        case STREAM_INFO_BUF_ALIGNMENT: info.sz(1); break;
        default: fail(GC_ERR_NOT_IMPLEMENTED, "Unsupported stream information");
    }
}
void impl_DSGetBufferInfo(Lock&, DS_HANDLE h, BUFFER_HANDLE bh, BUFFER_INFO_CMD cmd, INFO_ARGS) {
    auto b = buffer(h, bh); auto info = INFO;
    switch (cmd) {
        case BUFFER_INFO_BASE: info.ptr(b->data); break;
        case BUFFER_INFO_SIZE: info.sz(b->capacity); break;
        case BUFFER_INFO_USER_PTR: info.ptr(b->user); break;
        case BUFFER_INFO_TIMESTAMP: case BUFFER_INFO_TIMESTAMP_NS: info.u64(b->timestamp); break;
        case BUFFER_INFO_NEW_DATA: info.flag(b->fresh); b->fresh = false; break;
        case BUFFER_INFO_IS_QUEUED: info.flag(b->queued); break;
        case BUFFER_INFO_IS_ACQUIRING: case BUFFER_INFO_IS_INCOMPLETE: case BUFFER_INFO_DATA_LARGER_THAN_BUFFER: case BUFFER_INFO_CONTAINS_CHUNKDATA: case BUFFER_INFO_IS_COMPOSITE: info.flag(false); break;
        case BUFFER_INFO_TLTYPE: info.text("Custom"); break;
        case BUFFER_INFO_SIZE_FILLED: case BUFFER_INFO_DATA_SIZE: info.sz(b->filled); break;
        case BUFFER_INFO_WIDTH: info.sz(b->width); break;
        case BUFFER_INFO_HEIGHT: case BUFFER_INFO_DELIVERED_IMAGEHEIGHT: info.sz(b->height); break;
        case BUFFER_INFO_XOFFSET: case BUFFER_INFO_YOFFSET: case BUFFER_INFO_XPADDING: case BUFFER_INFO_YPADDING: case BUFFER_INFO_IMAGEOFFSET: case BUFFER_INFO_DELIVERED_CHUNKPAYLOADSIZE: info.sz(0); break;
        case BUFFER_INFO_FRAMEID: info.u64(b->frame); break;
        case BUFFER_INFO_IMAGEPRESENT: info.flag(b->filled != 0); break;
        case BUFFER_INFO_PAYLOADTYPE: info.sz(PAYLOAD_TYPE_IMAGE); break;
        case BUFFER_INFO_PIXELFORMAT: info.u64(mono8); break;
        case BUFFER_INFO_PIXELFORMAT_NAMESPACE: info.u64(PIXELFORMAT_NAMESPACE_PFNC_32BIT); break;
        case BUFFER_INFO_CHUNKLAYOUTID: info.u64(0); break;
        case BUFFER_INFO_PIXEL_ENDIANNESS: info.i32(PIXELENDIANNESS_LITTLE); break;
        default: fail(GC_ERR_NOT_IMPLEMENTED, "Unsupported buffer information");
    }
}
void impl_DSGetBufferChunkData(Lock&, DS_HANDLE h, BUFFER_HANDLE bh, SINGLE_CHUNK_DATA*, size_t* count) { buffer(h, bh); assign(count, size_t(0)); }
void impl_DSGetNumBufferParts(Lock&, DS_HANDLE h, BUFFER_HANDLE bh, uint32_t* count) { buffer(h, bh); assign(count, uint32_t(0)); }

void impl_GCRegisterEvent(Lock&, EVENTSRC_HANDLE h, EVENT_TYPE type, EVENT_HANDLE* handle) {
    require(handle != nullptr); port(h); require(type == EVENT_NEW_BUFFER, GC_ERR_NOT_IMPLEMENTED, "Only new-buffer events are supported"); get(h, Kind::Stream);
    for (auto& e : objects) require(e.second->kind != Kind::Event || e.second->parent != h, GC_ERR_RESOURCE_IN_USE, "Event already registered");
    *handle = create(Kind::Event, h)->handle; get(h, Kind::Stream)->event = *handle;
}
void impl_GCUnregisterEvent(Lock&, EVENTSRC_HANDLE h, EVENT_TYPE type) {
    port(h); require(type == EVENT_NEW_BUFFER, GC_ERR_NOT_IMPLEMENTED);
    for (auto& e : objects) if (e.second->kind == Kind::Event && e.second->parent == h) { auto id = e.first; get(h, Kind::Stream)->event = nullptr; erase(id); return; }
    fail(GC_ERR_INVALID_ID, "Event is not registered");
}
void impl_EventKill(Lock&, EVENT_HANDLE h) { ++get(h, Kind::Event)->epoch; changed.notify_all(); }
void impl_EventFlush(Lock&, EVENT_HANDLE h) {
    auto e = get(h, Kind::Event); auto s = get(e->parent, Kind::Stream);
    for (auto bh : s->output) get(bh, Kind::Buffer)->queued = false;
    s->output.clear();
}
void impl_EventGetInfo(Lock&, EVENT_HANDLE h, EVENT_INFO_CMD cmd, INFO_ARGS) {
    auto e = get(h, Kind::Event); auto info = INFO;
    switch (cmd) {
        case EVENT_EVENT_TYPE: info.i32(EVENT_NEW_BUFFER); break;
        case EVENT_NUM_IN_QUEUE: info.sz(get(e->parent, Kind::Stream)->output.size()); break;
        case EVENT_NUM_FIRED: info.u64(e->delivered); break;
        case EVENT_SIZE_MAX: case EVENT_INFO_DATA_SIZE_MAX: info.sz(sizeof(EVENT_NEW_BUFFER_DATA)); break;
        default: fail(GC_ERR_NOT_IMPLEMENTED, "Unsupported event information");
    }
}
void impl_EventGetDataInfo(Lock&, EVENT_HANDLE h, const void* input, size_t input_size, EVENT_DATA_INFO_CMD cmd, INFO_DATATYPE* type, void* output, size_t* size) {
    get(h, Kind::Event); require(input && input_size == sizeof(EVENT_NEW_BUFFER_DATA));
    if (cmd == EVENT_DATA_VALUE) { if (type) *type = INFO_DATATYPE_BUFFER; bytes(output, size, input, input_size); }
    else if (cmd == EVENT_DATA_NUMID) INFO.u64(EVENT_NEW_BUFFER);
    else if (cmd == EVENT_DATA_ID) INFO.text("NewBuffer");
    else fail(GC_ERR_NOT_IMPLEMENTED, "Unsupported event data information");
}
int64_t circle_position(uint64_t frame, int64_t speed, uint32_t initial,
                        uint32_t extent, uint32_t radius) {
    const int64_t span = int64_t(extent) - 1 - 2 * radius;
    if (span <= 0) return extent / 2;
    const int64_t period = 2 * span;
    const int64_t origin = std::clamp(int64_t(initial) - radius, int64_t(0), span);
    // Reduce the frame counter before multiplication to avoid overflow on long runs.
    int64_t phase = (origin + int64_t((frame - 1) % uint64_t(period)) * speed) % period;
    if (phase < 0) phase += period;
    return radius + (phase <= span ? phase : period - phase);
}
void draw_circles(uint8_t* pixels, Object& d, uint64_t frame) {
    const auto& scene = *d.scene;
    const uint32_t width = d.width * scene.columns, height = d.height * scene.rows;
    const uint32_t requested = scene.circle_radius ? scene.circle_radius :
        std::max(1U, std::min(d.width, d.height) / 4);
    const uint32_t radius = std::min(requested, (std::min(width, height) - 1) / 2);
    const int64_t tile_x = int64_t(d.camera) * d.width, tile_y = int64_t(d.board) * d.height;
    const int64_t radius2 = int64_t(radius) * radius;
    // Only a linear background write: no full-scene image or lookup cache.
    std::memset(pixels, 0, size_t(d.width) * d.height);
    for (uint32_t i = 0; i < scene.circles; ++i) {
        const uint32_t initial_x = i == 0 ? width / 5 : width * 4 / 5;
        const uint32_t initial_y = i == 0 ? height / 5 : height * 4 / 5;
        const int64_t cx = circle_position(frame, i == 0 ? scene.step : -scene.step,
                                           initial_x, width, radius);
        const int64_t cy = circle_position(frame, i == 0 ? scene.step : 2 * scene.step,
                                           initial_y, height, radius);
        if (cx + radius < tile_x || cx - radius >= tile_x + d.width) continue;
        const int64_t top = std::max(tile_y, cy - radius);
        const int64_t bottom = std::min(tile_y + d.height - 1, cy + radius);
        // Apply simulated exposure to two colors, not to every background pixel.
        const uint8_t value = uint8_t(std::min(255.0, std::round((i == 0 ? 220 : 160) * d.exposure / 1000.0)));
        for (int64_t gy = top; gy <= bottom; ++gy) {
            const int64_t dy = gy - cy, remaining = radius2 - dy * dy;
            int64_t half = int64_t(std::sqrt(double(remaining)));
            // Exact integer boundary even if a platform's sqrt rounds an edge.
            while ((half + 1) * (half + 1) <= remaining) ++half;
            while (half * half > remaining) --half;
            const int64_t left = std::max(tile_x, cx - half);
            const int64_t right = std::min(tile_x + d.width - 1, cx + half);
            if (left <= right) {
                auto* row = pixels + size_t(gy - tile_y) * d.width;
                std::memset(row + (left - tile_x), value, size_t(right - left + 1));
            }
        }
    }
}
void fill(Object& b, Object& d, uint64_t frame) {
    b.width = d.width; b.height = d.height; b.frame = frame;
    b.filled = size_t(d.width) * d.height;
    require(b.capacity >= b.filled, GC_ERR_BUFFER_TOO_SMALL, "Image exceeds buffer capacity");
    auto pixels = static_cast<uint8_t*>(b.data);
    auto& scene = *d.scene;
    if (scene.pixels.empty()) {
        draw_circles(pixels, d, frame);
    } else {
        // Legacy file input remains available to the older image playback demos.
        const uint32_t canvas_width = d.width * scene.columns, canvas_height = d.height * scene.rows;
        const int64_t shift = (int64_t((frame - 1) % canvas_width) * scene.step) % canvas_width;
        std::vector<uint32_t> source_x(d.width);
        for (uint32_t x = 0; x < d.width; ++x) {
            int64_t gx = int64_t(d.camera) * d.width + x + shift;
            gx = (gx % canvas_width + canvas_width) % canvas_width;
            source_x[x] = uint32_t(uint64_t(gx) * scene.width / canvas_width);
        }
        for (uint32_t y = 0; y < d.height; ++y) {
            auto row = pixels + size_t(y) * d.width;
            const uint32_t gy = uint32_t(d.board) * d.height + y;
            const uint32_t sy = uint32_t(uint64_t(gy) * scene.height / canvas_height);
            const auto* source = scene.pixels.data() + size_t(sy) * scene.width;
            for (uint32_t x = 0; x < d.width; ++x) row[x] = source[source_x[x]];
        }
        if (d.exposure != 1000.0) {
            const double scale = d.exposure / 1000.0;
            std::array<uint8_t, 256> lut{};
            for (size_t i = 0; i < lut.size(); ++i) lut[i] = uint8_t(std::min(255.0, std::round(i * scale)));
            for (size_t i = 0; i < b.filled; ++i) pixels[i] = lut[pixels[i]];
        }
    }
    b.timestamp = uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count());
    b.fresh = true;
}
void generate_frame(Object& s, Object& d, Clock::time_point now) {
    if (!s.running || !d.active) return;
    // Exposure limits the achievable free-running period in this simple model.
    auto period = std::chrono::nanoseconds(uint64_t(std::max(1000000000.0 / d.fps, d.exposure * 1000.0)));
    uint64_t due = 0;
    if (d.trigger) {
        if (d.triggers == 0 || now < s.next) return;
        --d.triggers; due = 1; s.next = now + period;
    } else {
        if (now < s.next) return;
        due = uint64_t((now - s.next) / period) + 1;
        s.next += period * due;
    }
    if (s.remaining != std::numeric_limits<uint64_t>::max()) {
        due = std::min(due, s.remaining); s.remaining -= due;
        if (s.remaining == 0) s.running = false;
    }
    s.frame += due; d.frame = s.frame;
    // Late CPU deadlines and missing input buffers count as dropped frames.
    // Never overwrite a filled buffer or a buffer owned by the consumer.
    uint64_t lost = due - 1;
    if (s.queue.empty()) {
        ++lost; ++s.underrun;
    } else {
        auto b = get(s.queue.front(), Kind::Buffer);
        // Allocate queue space before removing the buffer from the input pool.
        s.output.push_back(b->handle);
        try { fill(*b, d, s.frame); }
        catch (...) { s.output.pop_back(); throw; }
        s.queue.pop_front(); ++s.generated;
        if (s.event) ++get(s.event, Kind::Event)->delivered;
    }
    s.dropped += lost; d.dropped = s.dropped;
    changed.notify_all();
}
void engine_main() {
    Lock lock(mutex);
    while (!engine_stop) {
        auto wake = Clock::time_point::max();
        for (auto& entry : objects) {
            auto& s = *entry.second;
            if (s.kind != Kind::Stream || !s.running) continue;
            auto d = get(s.remote, Kind::Remote);
            if (!d->active) continue;
            try { generate_frame(s, *d, Clock::now()); }
            catch (const Fault& e) { s.failure = e.code; s.failure_text = e.text; s.running = false; changed.notify_all(); }
            catch (const std::bad_alloc&) { s.failure = GC_ERR_OUT_OF_MEMORY; s.failure_text = "Frame generator ran out of memory"; s.running = false; changed.notify_all(); }
            catch (...) { s.failure = GC_ERR_ERROR; s.failure_text = "Frame generator failed"; s.running = false; changed.notify_all(); }
            if (s.running && (!d->trigger || d->triggers > 0)) wake = std::min(wake, s.next);
        }
        // Give API callers a chance even when image generation exceeds a period.
        if (wake <= Clock::now()) {
            lock.unlock(); std::this_thread::yield(); lock.lock();
        } else {
            changed.wait_until(lock, wake);
        }
    }
}
void impl_EventGetData(Lock& lock, EVENT_HANDLE h, void* output, size_t* size, uint64_t timeout) {
    auto e = get(h, Kind::Event); auto s = get(e->parent, Kind::Stream); auto d = get(s->remote, Kind::Remote);
    require(size != nullptr); size_t capacity = *size; *size = sizeof(EVENT_NEW_BUFFER_DATA);
    if (!output) return;
    require(capacity >= *size, GC_ERR_BUFFER_TOO_SMALL, "Event buffer too small");
    uint64_t event_epoch = e->epoch, stream_epoch = s->epoch;
    auto now = Clock::now();
    auto max_ms = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::time_point::max() - now).count();
    auto deadline = timeout >= uint64_t(max_ms) ? Clock::time_point::max() : now + std::chrono::milliseconds(timeout);
    for (;;) {
        require(!e->closed && !s->closed && !d->closed && e->epoch == event_epoch && s->epoch == stream_epoch, GC_ERR_ABORT, "Event wait was aborted");
        now = Clock::now();
        if (!s->output.empty()) {
            auto b = get(s->output.front(), Kind::Buffer);
            s->output.pop_front(); b->queued = false;
            ++s->delivered;
            EVENT_NEW_BUFFER_DATA result{b->handle, b->user}; std::memcpy(output, &result, sizeof(result)); return;
        }
        require(s->failure == GC_ERR_SUCCESS, s->failure, s->failure_text);
        require(now < deadline, GC_ERR_TIMEOUT, "Timed out waiting for a frame");
        changed.wait_until(lock, deadline);
    }
}
} // namespace vfg
#include "exports.inc"
