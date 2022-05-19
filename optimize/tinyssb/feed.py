import gc
from sys import implementation
from uos import ilistdir, mkdir
from uhashlib import sha256
from uctypes import (
    UINT8,
    ARRAY,
    struct,
    addressof,
    BIG_ENDIAN,
    UINT32,
    sizeof,
    bytearray_at,
)
from ubinascii import hexlify
from .packet import (
    PLAIN48,
    CHAIN20,
    ISCHILD,
    ISCONTN,
    MKCHILD,
    CONTDAS,
    UPDFILE,
    APPLYUP,
    WIRE_PACKET,
    create_apply_pkt,
    create_chain,
    create_child_pkt,
    create_parent_pkt,
    create_upd_pkt,
    from_var_int,
    PACKET,
    new_packet,
    pkt_from_wire,
    PKT_PREFIX,
)


# helps debugging in vim
if implementation.name != "micropython":
    from typing import Optional, List, Tuple


FEED = {
    "reserved": (0 | ARRAY, 12 | UINT8),
    "fid": (12 | ARRAY, 32 | UINT8),
    "parent_fid": (44 | ARRAY, 32 | UINT8),
    "parent_seq": 76 | UINT32,
    "anchor_seq": 80 | UINT32,
    "anchor_mid": (84 | ARRAY, 20 | UINT8),
    "front_seq": 104 | UINT32,
    "front_mid": (108 | ARRAY, 20 | UINT8),
}


# basic feed functions
# ------------------------------------------------------------------------------


get_log_fn = lambda fid: "_feeds/{}.log".format(hexlify(fid).decode())
get_header_fn = lambda fid: "_feeds/{}.head".format(hexlify(fid).decode())


# this has to be changed for pycom
def listdir(path: Optional[str]) -> List[str]:
    if path is None:
        return [name for name, _, _ in list(ilistdir())]
    else:
        return [name for name, _, _ in list(ilistdir(path))]


def create_feed(
    fid: bytearray,
    trusted_seq: int = 0,
    trusted_mid: Optional[bytearray] = None,
    parent_seq: int = 0,
    parent_fid: bytearray = bytearray(32),
) -> struct[FEED]:
    if trusted_mid is None:
        trusted_mid = fid[:20]  # tinyssb convention

    assert len(fid) == 32
    assert len(trusted_mid) == 20
    assert len(parent_fid) == 32

    # create header
    feed = struct(addressof(bytearray(sizeof(FEED))), FEED, BIG_ENDIAN)
    feed.fid[:] = fid
    feed.parent_fid[:] = parent_fid
    feed.parent_seq = parent_seq
    feed.anchor_seq = trusted_seq
    feed.anchor_mid[:] = trusted_mid
    feed.front_seq = trusted_seq
    feed.front_mid[:] = fid[:20]  # tinyssb convention

    save_header(feed)
    return feed


def create_child_feed(
    parent_feed: struct[FEED],
    parent_key: bytearray,
    child_fid: bytearray,
    child_key: bytearray,
) -> struct[FEED]:
    parent_pkt = create_parent_pkt(
        parent_feed.fid,
        (parent_feed.front_seq + 1).to_bytes(4, "big"),
        parent_feed.front_mid,
        child_fid,
        parent_key,
    )

    child_feed = create_feed(
        child_fid, parent_seq=parent_feed.front_seq, parent_fid=parent_feed.fid
    )

    child_payload = bytearray(48)
    child_payload[:32] = parent_feed.fid
    child_payload[32:36] = parent_feed.front_seq.to_bytes(4, "big")
    child_payload[36:] = sha256(
        bytearray_at(addressof(parent_pkt.wire[0]), sizeof(WIRE_PACKET))
    ).digest()[:12]

    child_pkt = create_child_pkt(child_fid, child_payload, child_key)

    # append both
    append_packet(child_feed, child_pkt)
    append_packet(parent_feed, parent_pkt)
    return child_feed


def create_contn_feed(
    ending_feed: struct[FEED], contn_fid: bytearray, contn_key: bytearray
) -> struct[FEED]:
    pass


def get_feed(fid: bytearray) -> struct[FEED]:
    # reserve memory for header
    feed_header = bytearray(128)
    # read file
    f = open(get_header_fn(fid), "rb")
    feed_header[:] = f.read(128)
    f.close()

    # create struct
    feed = struct(addressof(feed_header), FEED, BIG_ENDIAN)
    return feed


def get_wire(feed: struct[FEED], i: int) -> bytearray:
    # transform negative indices
    if i < 0:
        i = feed.front_seq + i + 1

    # check if index is valid
    anchor_seq = feed.anchor_seq
    if i > feed.front_seq or i <= anchor_seq:
        raise IndexError

    # get wire packet
    relative_i = i - anchor_seq
    del anchor_seq
    wire_array = bytearray(128)
    f = open(get_log_fn(feed.fid), "rb")
    f.seek(128 * (relative_i - 1))  # -1 because header is in separate file
    wire_array[:] = f.read(128)
    f.close()

    return wire_array


def get_payload(feed: struct[FEED], i: int) -> bytearray:
    wire_array = get_wire(feed, i)
    gc.collect()

    # maybe direct array access instead?
    wpkt = struct(addressof(wire_array), WIRE_PACKET, BIG_ENDIAN)
    if wpkt.type != CHAIN20.to_bytes(1, "big"):
        return wpkt.payload

    # unwrap chain
    # get length
    content_size, num_bytes = from_var_int(wpkt.payload)
    if content_size <= 27:
        return wpkt.payload[1 : 1 + content_size]
    content_array = bytearray(content_size)
    current_i = 28 - num_bytes
    content_array[:current_i] = wpkt.payload[num_bytes:-20]

    ptr = wpkt.payload[-20:]
    del wpkt

    null_ptr = bytearray(20)
    while ptr != null_ptr:
        hex_ptr = hexlify(ptr).decode()
        file_name = "_blobs/{}/{}".format(hex_ptr[:2], hex_ptr[2:])
        blob_array = bytearray(128)
        f = open(file_name, "rb")
        blob_array[:] = f.read(128)
        f.close()
        del file_name
        ptr = blob_array[108:]

        # fill in and get next pointer
        if ptr == null_ptr:
            content_array[current_i:] = blob_array[8 : content_size - current_i + 8]
        else:
            content_array[current_i : current_i + 100] = blob_array[8:108]
            current_i += 100
        del blob_array

    return content_array


def save_header(feed: struct[FEED]) -> None:
    f = open(get_header_fn(feed.fid), "wb")
    f.write(bytearray_at(addressof(feed), sizeof(FEED)))
    f.close()


def append_packet(feed: struct[FEED], pkt: struct[PACKET]) -> None:
    # TODO: check if feed has ended?
    f = open(get_log_fn(feed.fid), "ab")
    f.seek(0, 2)  # move to end of file
    f.write(bytearray_at(addressof(pkt.wire[0]), sizeof(WIRE_PACKET)))
    f.close()

    # update header
    feed.front_mid[:] = pkt.mid
    del pkt
    feed.front_seq += 1
    save_header(feed)


def append_bytes(feed: struct[FEED], payload: bytearray, key: bytearray) -> None:
    payload_len = len(payload)
    assert payload_len <= 48

    if payload_len < 48:
        # pad content to 48B
        padded_payload = bytearray(48)
        padded_payload[:payload_len] = payload
        del payload
        payload = padded_payload
        del padded_payload

    pkt_type = PLAIN48.to_bytes(1, "big")
    seq = (feed.front_seq + 1).to_bytes(4, "big")
    pkt = new_packet(
        feed.fid,
        seq,
        feed.front_mid,
        payload,
        pkt_type,
        key,
    )
    append_packet(feed, pkt)


def append_blob(feed: struct[FEED], payload: bytearray, key: bytearray) -> None:
    pkt, blobs = create_chain(
        feed.fid, feed.front_seq.to_bytes(4, "big"), feed.front_mid, payload, key
    )

    ptr = hexlify(pkt.wire[0].payload[-20:]).decode()
    # save blob files
    for blob in blobs:
        dir_name = ptr[:2]
        file_name = ptr[2:]
        del ptr

        if dir_name not in listdir("_blobs"):
            mkdir("_blobs/{}".format(dir_name))

        # write blob
        f = open("_blobs/{}/{}".format(dir_name, file_name), "wb")
        f.write(bytearray_at(addressof(blob), sizeof(blob)))
        f.close()

        # get next ptr
        ptr = hexlify(blob.pointer).decode()

    del blobs
    assert ptr == "0000000000000000000000000000000000000000"
    # append packet to feed
    append_packet(feed, pkt)


def verify_and_append_bytes(feed: struct[FEED], wpkt: bytearray) -> None:
    pkt = pkt_from_wire(
        feed.fid, feed.front_seq.to_bytes(4, "big"), feed.front_mid, wpkt
    )

    if pkt is None:
        print("verification of packet failed")
        return

    append_packet(feed, pkt)


def get_parent(feed: struct[FEED]) -> Optional[bytearray]:
    if feed.anchor_seq != 0 or feed.front_mid < 1:
        return None
    wire = get_wire(feed, 1)

    # check type
    if wire[15:16] != ISCHILD.to_bytes(1, "big"):
        return None

    # return parent fid
    return wire[16:48]


def get_children(feed: struct[FEED]) -> List[bytearray]:
    # has to iterate over entire feed, avoid
    children = []
    mk_child = MKCHILD.to_bytes(1, "big")
    for i in range(feed.anchor_seq + 1, feed.front_seq + 1):
        wpkt = get_wire(feed, i)
        if wpkt[15:16] == mk_child:
            children.append(wpkt[16:48])

    return children


def get_contn(feed: struct[FEED]) -> Optional[bytearray]:
    if feed.front_seq < 1:
        return None

    wpkt = get_wire(feed, -1)
    if wpkt[15:16] == CONTDAS.to_bytes(1, "big"):
        return wpkt[16:48]

    return None


def get_prev(feed: struct[FEED]) -> Optional[bytearray]:
    if feed.anchor_seq != 0:
        return None

    wpkt = get_wire(feed, 1)
    if wpkt[15:16] == ISCONTN.to_bytes(1, "big"):
        return wpkt[16:48]

    return None


def get_next_dmx(feed: struct[FEED]) -> bytearray:
    dmx = bytearray(64)
    dmx[:8] = PKT_PREFIX
    dmx[8:40] = feed.fid
    dmx[40:44] = (feed.front_seq + 1).to_bytes(4, "big")
    dmx[44:64] = feed.front_mid
    return sha256(dmx).digest()[:7]


def waiting_for_blob(feed: struct[FEED]) -> Optional[bytearray]:
    if feed.front_seq < 1:
        return None

    # check front packet
    wpkt = get_wire(feed, -1)
    if wpkt[15:16] != CHAIN20.to_bytes(1, "BIG"):
        return None

    ptr = wpkt[16:36]
    null_ptr = bytearray(20)
    while ptr != null_ptr:
        hex_ptr = hexlify(ptr).decode()
        file_name = "_blobs/{}/{}".format(hex_ptr[:2], hex_ptr[2:])

        # check if file exists
        try:
            blob = bytearray(128)
            f = open(file_name, "rb")
            blob[:] = f.read(128)
            f.close()
        except Exception:
            # does not exist yet, return pointer
            del blob
            return ptr

        ptr[:] = blob[-20:]
        del blob

    return None


def verify_and_append_blob(feed: struct[FEED], blob: bytearray) -> None:
    assert len(blob) == 128
    # TODO: maybe skip check if already done by dmx check when receiving?
    blob_hash = sha256(blob[8:]).digest()[:20]
    if blob_hash != waiting_for_blob(feed):
        # not waiting for this blob
        return

    # save blob file
    hex_blob = hexlify(blob_hash).digest()
    file_name = "_blobs/{}/{}".format(hex_blob[:2], hex_blob[2:])
    f = open(file_name, "wb")
    f.write(blob)
    f.close()


def get_want(feed: struct[FEED]) -> bytearray:
    want_dmx = bytearray(7)
    want_dmx[:] = sha256(feed.fid + b"want").digest()[:7]

    # check whether blob or packet is missing
    # TODO: this could be inefficient for long blob chains
    blob_ptr = waiting_for_blob(feed)
    if blob_ptr is None:
        # packet missing
        want = bytearray(43)
        want[:7] = want_dmx
        want[7:39] = feed.fid
        want[39:] = (feed.front_seq + 1).to_bytes(4, "big")
        return want
    else:
        want = bytearray(63)
        want[:7] = want_dmx
        want[7:39] = feed.fid
        want[39:43] = feed.front_seq.to_bytes(4, "big")
        want[43:] = blob_ptr
        return want


def add_upd_file_name(
    feed: struct[FEED], file_name: str, key: bytearray, v_number: int = 0
) -> None:
    fn_array = bytearray(file_name.encode())
    vn_array = bytearray(v_number.to_bytes(4, "big"))
    pkt = create_upd_pkt(
        feed.fid,
        (feed.front_seq + 1).to_bytes(4, "big"),
        feed.front_mid,
        fn_array,
        vn_array,
        key,
    )

    append_packet(feed, pkt)


def get_upd_file_name(feed: struct[FEED]) -> Optional[Tuple[str, int]]:
    # assumes that the upp packet is at position 2 in the feed!
    wpkt = get_wire(feed, 2)
    # check type
    if wpkt[15:16] != UPDFILE.to_bytes(1, "big"):
        return None

    # extract info
    fn_len, n_bytes = from_var_int(wpkt[16:64])  # payload
    offset = 16 + n_bytes
    offset2 = offset + fn_len
    file_name = wpkt[offset:offset2].decode()
    del offset
    v_num = int.from_bytes(wpkt[offset2 : offset2 + 3], "big")
    del offset2

    return file_name, v_num


def add_apply(
    feed: struct[FEED], file_fid: bytearray, v_num: int, key: bytearray
) -> None:
    vn_arr = bytearray(v_num.to_bytes(4, "big"))
    pkt = create_apply_pkt(
        feed.fid,
        (feed.front_seq + 1).to_bytes(4, "big"),
        feed.front_mid,
        file_fid,
        vn_arr,
        key,
    )

    append_packet(feed, pkt)


def get_newest_apply(feed: struct[FEED], file_fid: bytearray) -> Optional[int]:
    # TODO: can this be improved? Naïve iterating over feed...
    applyup = APPLYUP.to_bytes(1, "big")
    for i in range(feed.front_seq, feed.anchor_seq, -1):
        wpkt = get_wire(feed, i)
        if wpkt[15:16] == applyup:
            if wpkt[16:48] == file_fid:
                return int.from_bytes(wpkt[48:52], "big")
        del wpkt

    return None


# less relevant functions
# ------------------------------------------------------------------------------


def to_string(feed: struct[FEED]) -> str:
    anchor_seq = feed.anchor_seq
    front_seq = feed.front_seq
    title = "".join([hexlify(feed.fid).decode()[:8], "..."])
    length = front_seq - anchor_seq
    separator = "".join([("+-----" * (length + 1)), "+"])
    numbers = "   {}  ".format(anchor_seq)
    feed_str = "| HDR |"

    for i in range(anchor_seq + 1, front_seq + 1):
        numbers = "".join([numbers, "   {}  ".format(i)])
        pkt_type = int.from_bytes(get_wire(feed, i)[15:16], "big")

        if pkt_type == PLAIN48:
            feed_str = "".join([feed_str, " P48 |"])
        if pkt_type == CHAIN20:
            feed_str = "".join([feed_str, " C20 |"])
        if pkt_type == ISCHILD:
            feed_str = "".join([feed_str, " ICH |"])
        if pkt_type == ISCONTN:
            feed_str = "".join([feed_str, " ICN |"])
        if pkt_type == MKCHILD:
            feed_str = "".join([feed_str, " MKC |"])
        if pkt_type == CONTDAS:
            feed_str = "".join([feed_str, " CTD |"])
        if pkt_type == UPDFILE:
            feed_str = "".join([feed_str, " UPD |"])
        if pkt_type == APPLYUP:
            feed_str = "".join([feed_str, " APP |"])

    return "\n".join([title, numbers, separator, feed_str, separator])