"""Laya, a fast local classifier, as deskhand's guard uses it.

server runs the shared local laya-serve process: it installs it, starts it on
demand and stops it when idle. client asks it one of the guard's questions and
returns a calibrated probability. evaluate measures it on labelled cases and
fits the temperatures and thresholds the guard reads. cases collects cases from
past runs and has Claude label them. Laya itself is a separate install, made by
`deskhand laya setup`, so PyTorch never enters deskhand's own environment.
"""
