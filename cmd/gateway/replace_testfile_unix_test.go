//go:build !windows

package main

import "os"

func replaceTestFile(source, destination string) error {
	return os.Rename(source, destination)
}
