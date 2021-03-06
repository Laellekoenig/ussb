from .util import PYCOM, to_var_int
from math import ceil
from micropython import const
from pure25519 import VerifyingKey, SigningKey
from sys import byteorder, implementation
from uctypes import (
    ARRAY,
    BIG_ENDIAN,
    PTR,
    UINT8,
    addressof,
    bytearray_at,
    sizeof,
    struct,
)
from uhashlib import sha256


# helps with debugging in vim
if implementation.name != "micropython":
    from typing import Optional, Tuple, List


# packet types
PLAIN48 = const(0x00)
CHAIN20 = const(0x01)
ISCHILD = const(0x02)
ISCONTN = const(0x03)
MKCHILD = const(0x04)
CONTDAS = const(0x05)
ACKNLDG = const(0x06)
UPDFILE = const(0x07)
APPLYUP = const(0x08)
FOKTREE = const(0x09)
SESTREE = const(0x10)
PKTFORK = const(0x11)


PKT_PREFIX = bytearray(b"tiny-v02")


# struct definitions
WIRE_PACKET = {
    "reserved": (0 | ARRAY, 8 | UINT8),
    "dmx": (8 | ARRAY, 7 | UINT8),
    "type": (15 | ARRAY, 1 | UINT8),
    "payload": (16 | ARRAY, 48 | UINT8),
    "signature": (64 | ARRAY, 64 | UINT8),
}


PACKET = {
    "wire": (0 | PTR, WIRE_PACKET),  # 12B pointer
    "fid": (12 | ARRAY, 32 | UINT8),
    "seq": (44 | ARRAY, 4 | UINT8),
    "prev_mid": (48 | ARRAY, 20 | UINT8),
    "mid": (68 | ARRAY, 20 | UINT8),
}


BLOB = {
    "reserved": (0 | ARRAY, 8 | UINT8),
    "payload": (8 | ARRAY, 100 | UINT8),
    "pointer": (108 | ARRAY, 20 | UINT8),
}


# struct methods
def new_packet(
    fid: bytearray,
    seq: bytearray,
    prev_mid: bytearray,
    payload: bytearray,
    pkt_type: bytearray,
    key: bytearray,
) -> struct[PACKET]:
    """
    Creates an instance of the PACKET struct for the given data.
    The packet consists of a WIRE_PACKET (pointer) and the 'virtual' information.
    It is signed using pure25519 with the given key.
    """
    assert len(fid) == 32
    assert len(seq) == 4
    assert len(prev_mid) == 20
    assert len(payload) == 48
    assert len(pkt_type) == 1
    assert len(key) == 32

    # create wire packet
    wpkt = struct(addressof(bytearray(sizeof(WIRE_PACKET))), WIRE_PACKET, BIG_ENDIAN)
    wpkt.reserved[:] = PKT_PREFIX
    wpkt.payload[:] = payload[:]
    wpkt.type[:] = pkt_type[:]

    # create packet
    pkt = struct(addressof(bytearray(sizeof(PACKET))), PACKET, BIG_ENDIAN)
    # add pointer to wire packet
    bytearray_at(addressof(pkt), 12)[:] = addressof(wpkt).to_bytes(12, byteorder)
    pkt.fid[:] = fid
    pkt.seq[:] = seq
    pkt.prev_mid[:] = prev_mid

    # calculate full name
    # reserve memory for everything in advance
    full_array = bytearray(184)
    full_array[:8] = wpkt.reserved
    full_array[8:40] = fid
    full_array[40:44] = seq
    full_array[44:64] = prev_mid

    # fill in dmx value
    wpkt.dmx[:] = sha256(full_array[:64]).digest()[:7]

    # calculate expanded packet
    full_array[64:71] = wpkt.dmx
    full_array[71:72] = pkt_type
    full_array[72:120] = payload

    # calculate full packet
    skey = SigningKey(bytes(key))
    full_array[120:] = skey.sign(bytes(full_array[:120]))
    wpkt.signature[:] = full_array[120:]

    # calculate message id
    pkt.mid[:] = sha256(full_array).digest()[:20]
    return pkt


def pkt_from_wire(
    fid: bytearray, seq: bytearray, prev_mid: bytearray, pkt_wire: bytearray
) -> Optional[struct[PACKET]]:
    """
    Constructs a PACKET struct from the given wire packet (as bytearray).
    The signature of this packet is checked. If it cannot be confirmed,
    None is returned.
    The check is currently skipped on pycom devices, since the pure25519
    library is not optimized and leads to stack overflows on these devices.
    """
    assert len(fid) == 32
    assert len(seq) == 4
    assert len(prev_mid) == 20
    assert len(pkt_wire) == 128

    # construct WIRE_PACKET struct from bytearray
    wpkt = struct(addressof(pkt_wire), WIRE_PACKET, BIG_ENDIAN)

    # construct pkt
    pkt = struct(addressof(bytearray(sizeof(PACKET))), PACKET, BIG_ENDIAN)
    # add pointer to wire packet
    bytearray_at(addressof(pkt), 12)[:] = addressof(wpkt).to_bytes(12, byteorder)
    pkt.fid[:] = fid
    pkt.seq[:] = seq
    pkt.prev_mid[:] = prev_mid

    # compute block name
    full_array = bytearray(184)
    full_array[:8] = wpkt.reserved
    full_array[8:40] = fid
    full_array[40:44] = seq
    full_array[44:64] = prev_mid

    # expand block name
    full_array[64:71] = wpkt.dmx
    full_array[71:72] = wpkt.type
    full_array[72:120] = wpkt.payload

    # verify signature
    if not PYCOM:
        # do not verify on pycom devices -> stack overflow (pure25519 is not optimized)
        vkey = VerifyingKey(bytes(fid))
        try:
            vkey.verify(bytes(wpkt.signature), bytes(full_array[:120]))
        except Exception as e:
            print(e)
            return None

    # verified packet, fill in signature
    full_array[120:] = wpkt.signature

    # calculate mid
    bytearray_at(addressof(pkt) + 68, 20)[:] = sha256(full_array).digest()[:20]
    return pkt


def create_genesis_pkt(
    fid: bytearray, payload: bytearray, skey: bytearray
) -> struct[PACKET]:
    """
    Creates an instance of a genesis packet.
    This packet has the sequence number 1 and contains the given payload.
    """
    seq = bytearray((1).to_bytes(4, "big"))
    pkt_type = bytearray(PLAIN48.to_bytes(1, "big"))
    return new_packet(fid, seq, fid[:20], payload, pkt_type, skey)


def create_parent_pkt(
    fid: bytearray,
    seq: bytearray,
    prev_mid: bytearray,
    child_fid: bytearray,
    skey: bytearray,
) -> struct[PACKET]:
    """
    Creates an instance of a parent packet (type MKCHILD).
    The payload of this packet contains the feed ID of the child feed.
    """
    pkt_type = bytearray(MKCHILD.to_bytes(1, "big"))
    payload = bytearray(48)
    payload[:32] = child_fid
    return new_packet(fid, seq, prev_mid, payload, pkt_type, skey)


def create_child_pkt(
    fid: bytearray, payload: bytearray, skey: bytearray
) -> struct[PACKET]:
    """
    Creates an instance of a child packet (type ISCHILD).
    The payload contains the feed ID of the parent feed.
    """
    seq = bytearray((1).to_bytes(4, "big"))
    prev_mid = fid[:20]
    pkt_type = bytearray(ISCHILD.to_bytes(1, "big"))
    return new_packet(fid, seq, prev_mid, payload, pkt_type, skey)


def create_end_pkt(
    fid: bytearray,
    seq: bytearray,
    prev_mid: bytearray,
    contn_fid: bytearray,
    skey: bytearray,
) -> struct[PACKET]:
    """
    Creates an instance of an "end" packet (type CONTDAS).
    The payload contains the feed ID of the continuation feed.
    """
    payload = bytearray(48)
    payload[:32] = contn_fid
    pkt_type = bytearray(CONTDAS.to_bytes(1, "big"))
    return new_packet(fid, seq, prev_mid, payload, pkt_type, skey)


def create_contn_pkt(
    fid: bytearray, payload: bytearray, skey: bytearray
) -> struct[PACKET]:
    """
    Creates an instance of a continuation packet (type ISCONTN).
    This packet contains the given payload.
    """
    seq = bytearray((1).to_bytes(4, "big"))
    prev_mid = fid[:20]
    pkt_type = bytearray(ISCONTN.to_bytes(1, "big"))
    return new_packet(fid, seq, prev_mid, payload, pkt_type, skey)


def create_upd_pkt(
    fid: bytearray,
    seq: bytearray,
    prev_mid: bytearray,
    file_name: bytearray,
    v_number: bytearray,
    key: bytearray,
) -> struct[PACKET]:
    """
    Creates an instance of an update packet (type UPDFILE).
    The payload of this packet contains the base version number
    (encoded as 4 big endian bytes) and the file name of the monitored file
    (max length 43B).

     <---------- 1B -----------> <------- 4B --------> < max 43B >
    +---------------------------+---------------------+-----------+
    | VarInt: length(file_name) | Base Version Number | File Name |
    +---------------------------+---------------------+-----------+
    """
    assert len(file_name) < 44, "file name too long: {}".format(file_name)
    payload = bytearray(48)
    fn_len = len(file_name)
    payload[:1] = to_var_int(fn_len)
    payload[1 : fn_len + 1] = file_name
    payload[fn_len + 1 : fn_len + 5] = v_number
    pkt_type = bytearray(UPDFILE.to_bytes(1, "big"))
    return new_packet(fid, seq, prev_mid, payload, pkt_type, key)


def create_apply_pkt(
    fid: bytearray,
    seq: bytearray,
    prev_mid: bytearray,
    file_fid: bytearray,
    update_seq: bytearray,
    key: bytearray,
) -> struct[PACKET]:
    """
    Creates an instance of an apply packet (type APPLYUP).
    This packet contains the feed ID of the affected file feed ID and the
    version number of the update that is being applied. This version number
    is encoded as a 4B (big endian).
    """
    assert len(file_fid) == 32
    assert len(update_seq) == 4
    payload = bytearray(48)
    payload[:32] = file_fid
    payload[32:36] = update_seq
    pkt_type = bytearray(APPLYUP.to_bytes(1, "big"))
    return new_packet(fid, seq, prev_mid, payload, pkt_type, key)


def create_chain(
    fid: bytearray,
    seq: bytearray,
    prev_mid: bytearray,
    content: bytearray,
    key: bytearray,
) -> Tuple[struct[PACKET], List[struct[BLOB]]]:
    """
    Creates a blob chain containing the given payload (no size limit).
    The chain is returned as a tuple containing: (CHAIN20 PACKET, List of blob structs).
    """
    # prepare packet type for later
    pkt_type = CHAIN20.to_bytes(1, "big")
    content_len = len(content)

    if content_len <= 27:
        # fits into single chain20 packet
        payload = bytearray(48)
        payload[:1] = to_var_int(content_len)
        payload[1 : content_len + 1] = content
        # null pointer already at end
        return new_packet(fid, seq, prev_mid, payload, pkt_type, key), []

    # prepend var int length and add padding to next 100B
    var_int = to_var_int(content_len)
    vil = len(var_int)
    expected_num_blobs = ceil((content_len - 28) / 100)

    # prepare payload for packet
    payload = bytearray(48)
    payload[:vil] = var_int
    payload[vil:28] = content[: 28 - vil]
    # update content length
    content_len -= 28 - vil

    # prepare blob content
    blob_content = bytearray(expected_num_blobs * 100)
    blob_content[:content_len] = content[28 - vil :]
    del content

    chain = []

    # start with last blob
    back = len(blob_content)
    front = max(back - 100, 0)

    ptr = bytes(20)  # last pointer is null pointer
    while front >= 0:
        blob = struct(addressof(bytearray(sizeof(BLOB))), BLOB, BIG_ENDIAN)
        blob.payload[:] = blob_content[front:back]
        blob.pointer[:] = ptr
        chain.append(blob)
        # calculate pointer for next blob
        ptr = sha256(bytearray_at(addressof(blob) + 8, 120)).digest()[:20]
        # advance front and back
        back -= 100
        front -= 100

    # reverse chain
    chain.reverse()

    # fill pointer into header
    payload[28:] = ptr
    return new_packet(fid, seq, prev_mid, payload, pkt_type, key), chain
