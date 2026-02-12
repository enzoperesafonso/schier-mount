from configuration import MountConfig
from coordinates import MountCoordinates

conf = MountConfig()
coord = MountCoordinates(conf)


ra, dec = coord.radec_to_enc(20,30)

print((ra, dec))

print(coord.enc_to_radec(ra,dec))