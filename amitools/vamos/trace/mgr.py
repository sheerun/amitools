import logging
from amitools.vamos.log import log_mem, log_mem_int, log_instr
from amitools.vamos.label import LabelStruct, LabelLib, LabelSegment
from amitools.vamos.machine import CPUState, DisAsm
from amitools.vamos.machine.regs import *
from .mem import TraceMemory


class TraceManager(object):
  trace_val_str = ("%02x      ", "%04x    ", "%08x")

  def __init__(self, machine):
    self.machine = machine
    self.cpu = machine.get_cpu()
    self.label_mgr = machine.get_label_mgr()
    self.disasm = DisAsm(machine)
    # state
    self.mem_tracer = None

  def parse_config(self, cfg):
    if not cfg:
      return True
    if cfg.vamos_ram:
      self.setup_vamos_ram_trace()
    if cfg.memory:
      self.setup_cpu_mem_trace()
    if cfg.instr:
      with_regs = cfg.reg_dump
      self.setup_cpu_instr_trace(with_regs)
    return True

  def setup_vamos_ram_trace(self):
    mem = self.machine.get_mem()
    self.mem_tracer = TraceMemory(mem, self)
    if not log_mem_int.isEnabledFor(logging.INFO):
      log_mem_int.setLevel(logging.INFO)

  def setup_cpu_mem_trace(self):
    self.machine.set_cpu_mem_trace_hook(self.trace_cpu_mem)
    if not log_mem.isEnabledFor(logging.INFO):
      log_mem.setLevel(logging.INFO)

  def setup_cpu_instr_trace(self, with_regs):
    if not log_instr.isEnabledFor(logging.INFO):
      log_instr.setLevel(logging.INFO)
    cpu = self.cpu
    state = CPUState()
    if with_regs:
      def instr_hook():
        # add register dump
        state.get(cpu)
        res = state.dump()
        for r in res:
          log_instr.info(r)
        # disassemble line
        pc = cpu.r_reg(REG_PC)
        self.trace_code_line(pc)
    else:
      def instr_hook():
        # disassemble line
        pc = cpu.r_reg(REG_PC)
        self.trace_code_line(pc)
    self.machine.set_instr_hook(instr_hook)

  # trace callback from CPU core
  def trace_cpu_mem(self, mode, width, addr, value=0):
    self._trace_mem(log_mem, mode, width, addr, value)
    return 0

  def trace_int_mem(self, mode, width, addr, value=0,
                    text="", addon=""):
    self._trace_mem(log_mem_int, mode, width, addr, value, text, addon)

  def trace_int_block(self, mode, addr, size,
                      text="", addon=""):
    info, label = self._get_mem_info(addr)
    log_mem_int.info(
        "%s(B): %06x: +%06x   %6s  [%s] %s",
        mode, addr, size,
        text, info, addon)

  def trace_code_line(self, pc):
    label, sym, src, addon = self._get_disasm_info(pc)
    _, txt = self.disasm.disassemble(pc)
    if sym is not None:
      log_instr.info("%s%s:", " "*40, sym)
    if src is not None:
      log_instr.info("%s%s", " "*50, src)
    log_instr.info("%-40s  %06x    %-20s  %s" % (label, pc, txt, addon))

  # ----- internal -----

  def _get_disasm_info(self, addr):
    if not self.label_mgr:
      return "N/A", None, None, ""
    label = self.label_mgr.get_label(addr)
    sym = None
    src = None
    addon = ""
    if label:
      mem = "@%06x +%06x %s" % (label.addr, addr - label.addr, label.name)
      if isinstance(label, LabelSegment):
        sym, src = self._get_segment_info(label.segment, label.addr, addr)
      elif isinstance(label, LabelLib):
        delta, fd_name = self._get_lib_short_info(addr, label)
        mem += "(-%d)" % delta
        if fd_name:
          addon = "; " + fd_name
    else:
      mem = "N/A"
    return mem, sym, src, addon

  def _get_segment_info(self, segment, segment_addr, addr):
    rel_addr = addr - segment_addr
    sym = segment.find_symbol(rel_addr)
    info = segment.find_debug_line(rel_addr)
    if info is None:
      src = None
    else:
      f = info.get_file()
      src_file = f.get_src_file()
      src_line = info.get_src_line()
      src = "[%s:%d]" % (src_file, src_line)
    return sym, src

  def _trace_mem(self, log, mode, width, addr, value,
                 text="", addon=""):
    val = self.trace_val_str[width] % value
    info, label = self._get_mem_info(addr)
    if text == "" and addon == "" and label is not None:
      text, addon = self._get_label_extra(label, mode, addr, width, value)
    log.info("%s(%d): %06x: %s  %6s  [%s] %s",
             mode, 2**width, addr, val,
             text, info, addon)

  def _get_mem_info(self, addr, width=None):
    if not self.label_mgr:
      return "??", None
    label = self.label_mgr.get_label(addr)
    if label is not None:
      txt = "@%06x +%06x %s" % (label.addr, addr - label.addr, label.name)
      return txt, label
    else:
      return "??", None

  def _get_label_extra(self, label, mode, addr, width, value):
    if isinstance(label, LabelLib):
      text, addon = self._get_lib_extra(label, mode, addr, width, value)
      if text:
        return text, addon
    if isinstance(label, LabelStruct):
      return self._get_struct_extra(label, addr, width)
    else:
      return "", ""

  def _get_struct_extra(self, label, addr, width):
    delta = addr - label.struct_begin
    if delta >= 0 and delta < label.struct_size:
      struct = label.struct(None, addr)
      st, field, f_delta = struct.get_struct_field_for_offset(delta)

      type_name = struct.get_type_name()
      name = st.get_field_path_name(field)
      type_sig = field.type_sig
      addon = "%s+%d = %s(%s)+%d" % (type_name, delta,
                                     name, type_sig, f_delta)
      return "Struct", addon
    else:
      return "", ""

  op_jmp = 0x4ef9
  op_reset = 0x04e70

  def _get_fd_signature(self, fd, bias):
    if fd is not None:
      f = fd.get_func_by_bias(bias)
      if f is not None:
        return f.get_str()

  def _get_lib_extra(self, label, mode, addr, width, value):
    # inside jump table
    if addr < label.base_addr:
      delta = label.base_addr - addr
      slot = delta / 6
      rel = delta % 6
      if rel == 0:
        addon = "-%d  [%d]" % (delta, slot)
      else:
        addon = "-%d  [%d]+%d" % (delta, slot, rel)
      fd_str = self._get_fd_signature(label.fd, delta)
      if fd_str:
        addon += "  " + fd_str
      return "JUMP", addon
    else:
      return None, None

  def _get_fd_name(self, fd, bias):
    if fd is not None:
      f = fd.get_func_by_bias(bias)
      if f is not None:
        return f.get_name()

  def _get_lib_short_info(self, addr, label):
    """get '(-offset)FuncName' string if addr is in jump table of label"""
    if addr < label.base_addr:
      delta = label.base_addr - addr
      fd_name = self._get_fd_name(label.fd, delta)
      return delta, fd_name
