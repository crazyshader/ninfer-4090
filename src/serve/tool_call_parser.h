#pragma once

#include "serve/request.h"

#include <cstddef>
#include <string>
#include <string_view>
#include <vector>

namespace ninfer::serve {

struct ParsedToolCallOutput {
    bool is_tool_call_response = false;
    std::string content;
    std::vector<ToolCall> tool_calls;
};

// Extracts every well-formed <tool_call> block from a model reply, best effort.
//
// Each block is parsed on its own: one malformed block no longer discards the blocks
// around it. Text outside the blocks -- before, between, after -- plus the raw bytes of
// any block that failed to parse, are joined into `content` (trimmed). A malformed block
// is deliberately left visible there rather than dropped, since this path has no logger.
//
// If no block parses, the whole reply is returned verbatim as `content` with
// is_tool_call_response = false, which is what the caller treats as "plain text answer".
//
// Note the streaming path is not symmetric: ToolCallStreamFilter withholds everything from
// the first <tool_call> onward, so text recovered into `content` here is not published as a
// stream delta. Callers that stream see an empty content for tool responses.
ParsedToolCallOutput parse_qwen_tool_call_output(const std::string& text,
                                                 std::size_t max_tool_name_length);

// Incrementally publishes text that is provably outside a possible Qwen
// <tool_call> suffix. At terminal time, a valid tool response discards the
// buffered tool region; malformed/non-tool output flushes it verbatim.
class ToolCallStreamFilter {
public:
    std::string feed(std::string_view text);
    std::string finish(bool is_tool_call_response);

    [[nodiscard]] std::size_t emitted_bytes() const noexcept { return emitted_bytes_; }

private:
    std::string pending_;
    std::string tool_region_;
    std::size_t emitted_bytes_ = 0;
    bool saw_tool_marker_      = false;
    bool finished_             = false;
};

} // namespace ninfer::serve
