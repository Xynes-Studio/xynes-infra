package idle

import "syscall"

func socketChange(info *syscall.Stat_t) (int64, int64) {
	return info.Ctim.Sec, info.Ctim.Nsec
}
