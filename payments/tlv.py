"""Bounded definite-length BER-TLV subset used by the learning fixtures."""
from dataclasses import dataclass
import re

class DecodeError(ValueError):
    pass

@dataclass(frozen=True)
class Node:
    tag: str
    offset: int
    value: bytes
    children: tuple

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def decode(text: str, max_bytes=65536, max_depth=12) -> tuple:
    if not isinstance(text, str) or not re.fullmatch(r"[0-9a-fA-F\s]*", text):
        raise DecodeError("Input must contain only hexadecimal bytes and whitespace")
    compact = "".join(text.split())
    if len(compact) % 2 or len(compact) > max_bytes * 2:
        raise DecodeError("Odd hex length or input exceeds byte limit")
    data = bytes.fromhex(compact)
    if not data:
        raise DecodeError("Empty TLV input")

    def parse(start, end, depth):
        if depth > max_depth:
            raise DecodeError("Nesting limit exceeded")
        nodes = []
        pos = start
        while pos < end:
            offset = pos
            first = data[pos]
            pos += 1
            if first in (0, 255):
                raise DecodeError(f"Unsupported padding/reserved tag at offset {offset}")
            if first & 31 == 31:
                count = 0
                while True:
                    if pos >= end or count >= 3:
                        raise DecodeError(f"Truncated or oversized tag at offset {offset}")
                    octet = data[pos]
                    if count == 0 and octet & 127 == 0:
                        raise DecodeError(f"Invalid high-tag encoding at offset {offset}")
                    pos += 1
                    count += 1
                    if not octet & 128:
                        break
            tag = data[offset:pos].hex().upper()
            if pos >= end:
                raise DecodeError(f"Missing length at offset {offset}")
            length = data[pos]
            pos += 1
            if length & 128:
                count = length & 127
                if count == 0 or count > 3 or pos + count > end:
                    raise DecodeError(f"Unsupported or truncated length at offset {offset}")
                if data[pos] == 0:
                    raise DecodeError(f"Non-minimal length at offset {offset}")
                length = int.from_bytes(data[pos:pos+count], "big")
                pos += count
                if length < 128:
                    raise DecodeError(f"Non-minimal long length at offset {offset}")
            stop = pos + length
            if stop > end:
                raise DecodeError(f"Value exceeds container at offset {offset}")
            children = parse(pos, stop, depth+1) if first & 32 else ()
            nodes.append(Node(tag, offset, data[pos:stop], children))
            pos = stop
        return tuple(nodes)
    return parse(0, len(data), 0)


def flatten(nodes):
    return [node for root in nodes for node in root.walk()]


def inventory(nodes):
    # Values are deliberately never exported, including constructed container values.
    return [{"tag": n.tag, "offset": n.offset, "length": len(n.value),
             "constructed": bool(int(n.tag[:2], 16) & 32)} for n in flatten(nodes)]
