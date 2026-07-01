#!/usr/bin/env python3
import json
import os
import re


def fence_marker(line):
    indent = line[: len(line) - len(line.lstrip(" "))]
    if len(indent) > 3:
        return None

    content = line[len(indent) :]
    if content.startswith("```"):
        length = len(content) - len(content.lstrip("`"))
        suffix = content[length:]
        if "`" in suffix:
            return None
        return "`", length, suffix
    if content.startswith("~~~"):
        length = len(content) - len(content.lstrip("~"))
        return "~", length, content[length:]
    return None


def indented_code_line(line):
    return re.match(r"( {4,}| {0,3}\t)", line) is not None


def html_block_start(line):
    return re.match(r"</?[A-Za-z][A-Za-z0-9-]*([ \t>/].*)?$", line) is not None


def matching_code_span_end(line, start, length):
    index = start
    while index < len(line):
        if line[index] == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            if end - index == length:
                return end
            index = end
            continue
        index += 1
    return -1


def html_top_level_heading(line):
    if indented_code_line(line):
        return False
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if indent > 3:
        return False
    index = 0

    while index < len(stripped):
        if re.match(r"<h[12](?:[ \t>]|/?>|$)", stripped[index:], re.IGNORECASE):
            return True

        if stripped[index] == "\\" and index + 1 < len(stripped):
            index += 2
            continue

        if stripped[index] == "`":
            end = index
            while end < len(stripped) and stripped[end] == "`":
                end += 1
            length = end - index
            close = matching_code_span_end(stripped, end, length)
            if close != -1:
                index = close
            else:
                index = end
            continue

        index += 1

    return False


def thematic_break(line):
    return re.fullmatch(r" {0,3}([-*_])([ \t]*\1){2,}[ \t]*", line) is not None


def setext_heading_candidate(line):
    if indented_code_line(line):
        return False
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if indent > 3 or not stripped:
        return False
    if re.match(r"#{1,6}([ \t]+|$)", stripped):
        return False
    if stripped.startswith(">"):
        return False
    if re.match(r"[-*+]([ \t]+|$)", stripped):
        return False
    if re.match(r"\d+[.)]([ \t]+|$)", stripped):
        return False
    if thematic_break(line):
        return False
    if html_block_start(stripped):
        return False
    if re.match(r"\[[^\]]+\]:[ \t]*\S+", stripped):
        return False
    return True


def find_comment_start(line, start_index):
    index = start_index
    in_code_span = False
    code_span_len = 0

    while index < len(line):
        if line.startswith("<!--", index) and not in_code_span:
            return index

        if line[index] == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            length = end - index
            if in_code_span and length == code_span_len:
                in_code_span = False
                code_span_len = 0
            elif not in_code_span:
                in_code_span = True
                code_span_len = length
            index = end
            continue

        index += 1

    return -1


def strip_hidden_comments(markdown):
    lines = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    in_comment = False

    for line in markdown.splitlines():
        marker = fence_marker(line)
        if not in_comment and marker:
            char, length, suffix = marker
            if not in_fence:
                in_fence = True
                fence_char = char
                fence_len = length
            elif char == fence_char and length >= fence_len and not suffix.strip():
                in_fence = False
                fence_char = ""
                fence_len = 0
            lines.append(line)
            continue

        if in_fence:
            lines.append(line)
            continue

        if not in_comment and indented_code_line(line):
            lines.append(line)
            continue

        output = ""
        index = 0
        while index < len(line):
            if in_comment:
                end = line.find("-->", index)
                if end == -1:
                    index = len(line)
                else:
                    in_comment = False
                    index = end + 3
                continue

            start = find_comment_start(line, index)
            if start == -1:
                output += line[index:]
                break

            output += line[index:start]
            end = line.find("-->", start + 4)
            if end == -1:
                in_comment = True
                index = len(line)
            else:
                if re.fullmatch(r" {0,3}#{1,6}", output):
                    output += line[start : end + 3]
                index = end + 3

        if output.strip():
            lines.append(output)
        elif output or not in_comment:
            lines.append(output)

    return "\n".join(lines)


def first_content_line(markdown):
    for line in strip_hidden_comments(markdown).splitlines():
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        if stripped.startswith((" ##", "  ##", "   ##")):
            return stripped.lstrip(" ")
        return stripped
    return ""


def top_level_headings(markdown):
    headings = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    previous_heading_candidate = None

    for line in strip_hidden_comments(markdown).splitlines():
        marker = fence_marker(line)
        if marker:
            char, length, suffix = marker
            if not in_fence:
                in_fence = True
                fence_char = char
                fence_len = length
            elif char == fence_char and length >= fence_len and not suffix.strip():
                in_fence = False
                fence_char = ""
                fence_len = 0
            continue

        if in_fence:
            continue

        stripped = line.rstrip()
        if re.match(r" {0,3}#{1,2}([ \t]+|$)", stripped):
            headings.append(stripped.lstrip(" "))
            previous_heading_candidate = None
            continue
        if html_top_level_heading(stripped):
            headings.append(stripped.lstrip(" "))
            previous_heading_candidate = None
            continue

        if re.fullmatch(r" {0,3}(=+|-+)[ \t]*", stripped):
            if previous_heading_candidate:
                headings.append(previous_heading_candidate.lstrip(" "))
            previous_heading_candidate = None
            continue

        if setext_heading_candidate(stripped):
            previous_heading_candidate = stripped
        else:
            previous_heading_candidate = None

    return headings


def parse_layout(markdown):
    return {
        "first_content_line": first_content_line(markdown),
        "top_level_headings": "\n".join(top_level_headings(markdown)),
    }


def main():
    print(json.dumps(parse_layout(os.environ.get("PR_BODY", ""))))


if __name__ == "__main__":
    main()
