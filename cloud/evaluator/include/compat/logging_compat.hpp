/*!
 * @file logging_compat.hpp
 * @brief 轻量日志兼容层，提供与 glog 相同的 LOG/DLOG/CHECK 宏接口，
 *        输出到 stderr，无任何外部依赖。
 */
#pragma once
#include <cstdlib>
#include <iostream>
#include <sstream>

// ─── 日志级别标签 ────────────────────────────────────────────────────────────
#define SEVERITY_INFO    "I"
#define SEVERITY_WARNING "W"
#define SEVERITY_ERROR   "E"
#define SEVERITY_FATAL   "F"

// ─── 内部流辅助：写入一行后自动换行 ─────────────────────────────────────────
namespace logging_compat {

class LogStream {
public:
    LogStream(const char* severity, const char* file, int line, bool abort_on_dtor = false)
        : abort_(abort_on_dtor) {
        // 提取短文件名
        const char* slash = file;
        for (const char* p = file; *p; ++p) {
            if (*p == '/' || *p == '\\') slash = p + 1;
        }
        buf_ << "[" << severity << " " << slash << ":" << line << "] [uroad-eval] ";
    }

    template <typename T>
    LogStream& operator<<(const T& v) { buf_ << v; return *this; }

    ~LogStream() {
        std::cerr << buf_.str() << "\n";
        if (abort_) {
            std::cerr << "Aborting due to FATAL log.\n";
            std::abort();
        }
    }

private:
    std::ostringstream buf_;
    bool abort_;
};

// 空流：用于禁用 DLOG/VLOG 时吞掉输出
struct NullStream {
    template <typename T>
    NullStream& operator<<(const T&) { return *this; }
};

} // namespace logging_compat

// ─── 主要宏 ──────────────────────────────────────────────────────────────────
#define LOG(severity)  logging_compat::LogStream(SEVERITY_##severity, __FILE__, __LINE__, \
                           (std::string(SEVERITY_##severity) == "F"))

#ifndef NDEBUG
#  define DLOG(severity) LOG(severity)
#else
#  define DLOG(severity) logging_compat::NullStream()
#endif

// VLOG：默认禁用（level > 0 吞掉）
#define VLOG(level)    logging_compat::NullStream()

// ─── LOG_IF / DLOG_IF ────────────────────────────────────────────────────────
#define LOG_IF(severity, cond)  if (cond) LOG(severity)
#define DLOG_IF(severity, cond) if (cond) DLOG(severity)

// ─── CHECK 宏 ─────────────────────────────────────────────────────────────────
#define CHECK(cond)                                                        \
    if (!(cond)) logging_compat::LogStream(SEVERITY_FATAL, __FILE__, __LINE__, true) \
        << "CHECK failed: " #cond " "

#define CHECK_EQ(a, b) CHECK((a) == (b))
#define CHECK_NE(a, b) CHECK((a) != (b))
#define CHECK_LT(a, b) CHECK((a) <  (b))
#define CHECK_LE(a, b) CHECK((a) <= (b))
#define CHECK_GT(a, b) CHECK((a) >  (b))
#define CHECK_GE(a, b) CHECK((a) >= (b))

#define DCHECK(cond)      CHECK(cond)
#define DCHECK_EQ(a, b)   CHECK_EQ(a, b)
#define DCHECK_NE(a, b)   CHECK_NE(a, b)
#define DCHECK_LT(a, b)   CHECK_LT(a, b)
#define DCHECK_LE(a, b)   CHECK_LE(a, b)
#define DCHECK_GT(a, b)   CHECK_GT(a, b)
#define DCHECK_GE(a, b)   CHECK_GE(a, b)

// FLAGS_* 兼容：offline 工具不需要，提供空占位避免编译错误
#ifndef FLAGS_alsologtostderr
#  define FLAGS_alsologtostderr  (void(0))
#endif
#ifndef FLAGS_colorlogtostderr
#  define FLAGS_colorlogtostderr (void(0))
#endif
#ifndef FLAGS_minloglevel
#  define FLAGS_minloglevel      (void(0))
#endif
