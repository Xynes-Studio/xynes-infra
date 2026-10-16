package idle

import "syscall"

func socketChange(info *syscall.Stat_t) (int64, int64) {
	return info.Ctimespec.Sec, info.Ctimespec.Nsec
}
